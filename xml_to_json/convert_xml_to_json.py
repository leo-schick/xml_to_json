"""
(c) 2019 David Lee.

Author: David Lee
"""
import xml.etree.cElementTree as ET
import xmlschema
from collections import OrderedDict
import decimal
import json
import glob
from multiprocessing import Pool
import subprocess
import os
import gzip
import tarfile
import logging
import shutil
import sys
from zipfile import ZipFile
# import time

from xmlschema.exceptions import XMLSchemaValueError
from xmlschema.compat import ordered_dict_class

_logger = logging.getLogger(__name__)
_logger.setLevel(logging.DEBUG)


def json_decoder(obj):
    """
    :param obj: python data
    :return: converted type
    :raises:
    """
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S.%f')
    if isinstance(obj, set):
        return list(obj)
    raise TypeError(repr(obj) + " is not JSON serializable")


def nested_get(nested_dict, keys):
    """
    :param nested_dict: dictionary
    :param keys: list of keys
    :return: return back object
    """
    for key in keys:
        if isinstance(nested_dict, list):
            nested_dict = nested_dict[0][key]
        else:
            nested_dict = nested_dict[key]
    return nested_dict


class ParqConverter(xmlschema.XMLSchemaConverter):
    """
    XML Schema based converter class for Parquet friendly json.
    """

    def __init__(self, namespaces=None, dict_class=None, list_class=None, **kwargs):
        """
        :param namespaces: map from namespace prefixes to URI.
        :param dict_class: dictionary class to use for decoded data. Default is `dict`.
        :param list_class: list class to use for decoded data. Default is `list`.
        """
        kwargs.update(attr_prefix='', text_key=None, cdata_prefix=None)
        super(ParqConverter, self).__init__(
            namespaces, dict_class or ordered_dict_class, list_class, **kwargs
        )

    def __setattr__(self, name, value):
        """
        :param name: attribute name.
        :param value: attribute value.
        :raises XMLSchemaValueError: Schema validation error for this converter
        """
        if name in ('text_key', 'cdata_prefix') and value is not None:
            raise XMLSchemaValueError('Wrong value %r for the attribute %r of a %r.' % (value, name, type(self)))
        super(xmlschema.XMLSchemaConverter, self).__setattr__(name, value)

    @property
    def lossless(self):
        """
        :return: Returns back lossless property for this converter
        """
        return False

    def element_decode(self, data, xsd_element, level=0):
        """
        :param data: Decoded ElementData from an Element node.
        :param xsd_element: The `XsdElement` associated to decoded the data.
        :param level: 0 for root
        :return: A dictionary-based data structure containing the decoded data.
        """
        if data.attributes:
            self.attr_prefix = xsd_element.local_name
            result_dict = self.dict([(k, v) for k, v in self.map_attributes(data.attributes)])
        else:
            result_dict = self.dict()
        if xsd_element.type.is_simple() or xsd_element.type.has_simple_content():
            result_dict[xsd_element.local_name] = data.text if data.text is not None and data.text != "" else None

        if data.content:
            for name, value, xsd_child in self.map_content(data.content):
                if value:
                    if xsd_child.local_name:
                        name = xsd_child.local_name
                    else:
                        name = name[2 + len(xsd_child.namespace):]

                    if xsd_child.is_single():
                        if hasattr(xsd_child, 'type') and (xsd_child.type.is_simple() or xsd_child.type.has_simple_content()):
                            for k in value:
                                result_dict[k] = value[k]
                        else:
                            result_dict[name] = value
                    else:
                        if (xsd_child.type.is_simple() or xsd_child.type.has_simple_content()) and not xsd_child.attributes:
                            if len(xsd_element.findall("*")) == 1:
                                try:
                                    result_dict.append(list(value.values())[0])
                                except AttributeError:
                                    result_dict = self.list(value.values())
                            else:
                                try:
                                    result_dict[name].append(list(value.values())[0])
                                except KeyError:
                                    result_dict[name] = self.list(value.values())
                                except AttributeError:
                                    result_dict[name] = self.list(value.values())
                        else:
                            try:
                                result_dict[name].append(value)
                            except KeyError:
                                result_dict[name] = self.list([value])
                            except AttributeError:
                                result_dict[name] = self.list([value])
        if level == 0:
            return self.dict([(xsd_element.local_name, result_dict)])
        else:
            return result_dict


def open_file(zip, filename):
    """
    :param zip: whether to open a new file using gzip
    :param filename: name of new file
    :return: file handlers
    """
    if filename == '-':
        if zip:
            raise ValueError("zip is not supported from stdin")
        return os.fdopen(sys.stdout.fileno(), "wb")
    if zip:
        return gzip.open(filename, "wb")
    else:
        return open(filename, "wb")


def parse_root(xml_file, parent_xpath_list):
    """
    :param xml_file: xml file
    :param parent_xpath_list: xpath of parent
    :return: root and parent for xml snippet
    """
    parent = None
    currentxpath = []

    context = ET.iterparse(xml_file, events=("start", "end"))
    event, root = next(context)
    currentxpath.append(root.tag.split('}', 1)[-1])
    if currentxpath == parent_xpath_list:
        root.clear()
        parent = root
    else:
        for event, elem in context:
            if event == "start":
                currentxpath.append(elem.tag.split('}', 1)[-1])
                if currentxpath == parent_xpath_list:
                    elem.clear()
                    parent = elem
                    break
            if event == "end":
                elem.clear()
                del currentxpath[-1]
    if parent is None:
        root = None
    del context
    return (root, parent)


def parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip):
    """
    :param xml_file: xml file
    :param json_file: json file
    :param my_schema: xmlschema object
    :param output_format: jsonl or json
    :param xpath_list: xpath in array format
    :param root: xml root
    :param parent: xml_parent
    :param attribpaths_dict: captured parent root elements for attributes
    :param excludepaths_set: paths to exclude
    :param excludeparents_set: parent paths of excludes
    :param elem_active: keep or clear elem
    :param processed: data found and processed previously
    :param from_zip: if data is from a file in a zip archive
    :return: data found and processed
    """

    is_array = False
    excludeparent = None
    currentxpath = []

    context = ET.iterparse(xml_file, events=("start", "end"))
    # Parse XML
    for event, elem in context:
        if event == "start":
            currentxpath.append(elem.tag.split('}', 1)[-1])
            if currentxpath == xpath_list:
                elem_active = True

            currentxpath_key = tuple(currentxpath)

            if currentxpath_key in attribpaths_dict:
                new_elem = ET.Element(elem.tag)
                new_elem.attrib = elem.attrib

                if attribpaths_dict[currentxpath_key]['root'] is None:
                    attribpaths_dict[currentxpath_key]['attributes'] = nested_get(my_schema.to_dict(new_elem, process_namespaces=False, validation='skip'), currentxpath)
                else:
                    attribpaths_dict[currentxpath_key]['parent'].append(new_elem)
                    attribpaths_dict[currentxpath_key]['attributes'] = nested_get(my_schema.to_dict(attribpaths_dict[currentxpath_key]['root'], process_namespaces=False, validation='skip'), currentxpath)
                    attribpaths_dict[currentxpath_key]['parent'].remove(new_elem)
                if isinstance(attribpaths_dict[currentxpath_key]['attributes'], list):
                    attribpaths_dict[currentxpath_key]['attributes'] = attribpaths_dict[currentxpath_key]['attributes'][0]

            if currentxpath_key in excludeparents_set:
                excludeparent = elem

        if event == "end":
            if currentxpath == xpath_list:
                parent.append(elem)
                try:
                    my_dict = nested_get(my_schema.to_dict(root, process_namespaces=False, validation='skip'), xpath_list)
                    if isinstance(my_dict, list):
                        is_array = True
                        my_dict = my_dict[0]
                    if len(attribpaths_dict) > 0:
                        attrib_dict = dict()
                        for dict_value in attribpaths_dict.values():
                            if dict_value['attributes']:
                                attrib_dict.update(dict_value['attributes'])
                        my_dict = {**attrib_dict, **my_dict}

                    my_json = json.dumps(my_dict, default=json_decoder)

                    if not processed:
                        processed = True
                        if is_array and output_format == "json" and not from_zip:
                            json_file.write(bytes("[" + os.linesep, "utf-8"))
                        json_file.write(bytes(my_json, "utf-8"))
                    else:
                        if output_format == "json":
                            json_file.write(bytes("," + os.linesep + my_json, "utf-8"))
                        else:
                            json_file.write(bytes(os.linesep + my_json, "utf-8"))
                except Exception as ex:
                    _logger.debug(ex)
                    pass
                parent.remove(elem)
            if not elem_active:
                elem.clear()

            currentxpath_key = tuple(currentxpath)

            if currentxpath_key in excludepaths_set:
                excludeparent.remove(elem)

            del currentxpath[-1]

    if xpath_list:
        if is_array and output_format == "json" and not from_zip:
            json_file.write(bytes(os.linesep + "]", "utf-8"))
    else:
        my_dict = my_schema.to_dict(elem, process_namespaces=False, validation='skip')
        try:
            my_json = json.dumps(my_dict, default=json_decoder)
        except Exception as ex:
            _logger.debug(ex)
            pass
        if len(my_json) > 0:
            if not processed:
                processed = True
                json_file.write(bytes(my_json, "utf-8"))
            else:
                if output_format == "json":
                    json_file.write(bytes("," + os.linesep + my_json, "utf-8"))
                else:
                    json_file.write(bytes(os.linesep + my_json, "utf-8"))

    del context
    return processed


def parse_file(input_file, output_file, xsd_file, output_format, zip, xpath=None, attribpaths=None, excludepaths=None, target_path=None, server=None, delete_xml=None):
    """
    :param input_file: input file
    :param output_file: output file
    :param xsd_file: xsd file
    :param output_format: jsonl or json
    :param zip: zip save file
    :param xpath: whether to parse a specific xml path
    :param attribpaths: paths to capture attributes when used with xpath
    :param excludepaths: paths to exclude
    :param target_path: directory to save file
    :param server: optional server with hadoop client installed if current server does not have hadoop installed
    :param delete_xml: optional delete xml file after converting
    """

    _logger.debug("Generating schema from " + xsd_file)

    my_schema = xmlschema.XMLSchema(xsd_file, converter=ParqConverter)

    _logger.debug("Parsing " + input_file)

    _logger.debug("Writing to file " + output_file)

    xpath_list = None
    attribpaths_dict = dict()
    excludepaths_set = set()
    excludeparents_set = set()

    if excludepaths:
        excludepaths = excludepaths.split(",")
        excludepaths_list = [v.split("/")[1:] for v in excludepaths]
        excludepaths_set = {tuple(v) for v in excludepaths_list}
        excludeparents_set = {tuple(v[:-1]) for v in excludepaths_list}

    if xpath:
        xpath_list = xpath.split("/")[1:]

        if attribpaths:
            attribpaths_list = [v.split("/")[1:] for v in attribpaths.split(",")]
            attribpaths_dict = {tuple(v): {"root": None, "parent": None, "attributes": {}} for v in attribpaths_list}

        if tuple(xpath_list) in attribpaths_dict:
            del(attribpaths_dict[tuple(xpath_list)])

        xsd_elem = my_schema.find(xpath, namespaces=my_schema.namespaces)
        elem_active = False
    else:
        elem_active = True

    processed = False

    with open_file(zip, output_file) as json_file:

        root = None
        parent = None

        if input_file.endswith((".zip", ".tar.gz")) and output_format == "json":
            json_file.write(bytes("[" + os.linesep, "utf-8"))

        if input_file.endswith(".tar.gz"):
            zip_file = tarfile.open(input_file, 'r')
            zip_file_list = zip_file.getmembers()

            for member in zip_file_list:
                if xpath_list:
                    if root is None:
                        parent_xpath_list = xpath_list[:-1]
                        with zip_file.extractfile(member) as xml_file:
                            root, parent = parse_root(xml_file, parent_xpath_list)
                    if root is not None:
                        if attribpaths:
                            for k, v in attribpaths_dict.items():
                                attribpaths_dict[k]['attributes'] = {}
                                if v['root'] is None:
                                    parent_xpath_list = list(k)[:-1]
                                    with zip_file.extractfile(member) as xml_file:
                                        attribpaths_dict[k]['root'], attribpaths_dict[k]['parent'] = parse_root(xml_file, parent_xpath_list)

                        with zip_file.extractfile(member) as xml_file:
                            processed = parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=True)
                else:
                    with zip_file.extractfile(member) as xml_file:
                        processed = parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=True)

        elif input_file.endswith(".zip"):
            zip_file = ZipFile(input_file, 'r')
            zip_file_list = zip_file.infolist()

            for i in range(len(zip_file_list)):
                if xpath_list:
                    if root is None:
                        parent_xpath_list = xpath_list[:-1]
                        with zip_file.open(zip_file_list[i].filename) as xml_file:
                            root, parent = parse_root(xml_file, parent_xpath_list)
                    if root is not None:
                        if attribpaths:
                            for k, v in attribpaths_dict.items():
                                attribpaths_dict[k]['attributes'] = {}
                                if v['root'] is None:
                                    parent_xpath_list = list(k)[:-1]
                                    with zip_file.open(zip_file_list[i].filename) as xml_file:
                                        attribpaths_dict[k]['root'], attribpaths_dict[k]['parent'] = parse_root(xml_file, parent_xpath_list)

                        with zip_file.open(zip_file_list[i].filename) as xml_file:
                            processed = parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=True)
                else:
                    with zip_file.open(zip_file_list[i].filename) as xml_file:
                        processed = parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=True)
        
        elif input_file.endswith(".gz"):
            if xpath_list:
                if root is None:
                    parent_xpath_list = xpath_list[:-1]
                    with gzip.open(input_file) as xml_file:
                        root, parent = parse_root(xml_file, parent_xpath_list)

                if root is not None:
                    if attribpaths:
                        for k, v in attribpaths_dict.items():
                            parent_xpath_list = list(k)[:-1]
                            with gzip.open(input_file) as xml_file:
                                attribpaths_dict[k]['root'], attribpaths_dict[k]['parent'] = parse_root(xml_file, parent_xpath_list)
                    
                    with gzip.open(input_file) as xml_file:
                        processed = parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=False)
            else:
                with gzip.open(input_file) as xml_file:
                    processed = parse_xml(xml_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=False)

        else:
            if input_file == '-':
                _input_file = sys.stdin
            else:
                _input_file = input_file

            if xpath_list:
                if root is None:
                    parent_xpath_list = xpath_list[:-1]
                    root, parent = parse_root(input_file, parent_xpath_list)

                if root is not None:
                    if attribpaths:
                        for k, v in attribpaths_dict.items():
                            parent_xpath_list = list(k)[:-1]
                            attribpaths_dict[k]['root'], attribpaths_dict[k]['parent'] = parse_root(input_file, parent_xpath_list)

                    processed = parse_xml(_input_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=False)
            else:
                processed = parse_xml(_input_file, json_file, my_schema, output_format, xpath_list, root, parent, attribpaths_dict, excludepaths_set, excludeparents_set, elem_active, processed, from_zip=False)

        if input_file.endswith((".zip", ".tar.gz")) and output_format == "json":
            json_file.write(bytes(os.linesep + "]", "utf-8"))

    # Remove file if no json is generated
    if not processed:
        os.remove(output_file)
        _logger.debug("No data found in " + input_file)
        return

    if delete_xml:
        os.remove(input_file)

    if target_path and target_path.startswith("hdfs:"):
        _logger.debug("Moving " + output_file + " to " + target_path)
        if server:
            if subprocess.call(["ssh", server, "hadoop fs -put -f " + output_file + " " + target_path]) != 0:
                _logger.error("invalid target_path specified")
                sys.exit(1)
        else:
            if subprocess.call(["hadoop", "fs", "-put", "-f", output_file, target_path]) != 0:
                _logger.error("invalid target_path specified")
                sys.exit(1)

        os.remove(output_file)

    _logger.debug("Completed " + input_file)


def convert_xml_to_json(xsd_file=None, output_format="jsonl", server=None, target_path=None, zip=False, xpath=None, attribpaths=None, excludepaths=None, multi=1, no_overwrite=False, verbose="DEBUG", log=None, delete_xml=None, xml_files=None):
    """
    :param xsd_file: xsd file name
    :param output_format: jsonl or json
    :param server: optional server with hadoop client installed if current server does not have hadoop installed
    :param target_path: directory to save file
    :param zip: zip save file
    :param xpath: whether to parse a specific xml path
    :param attribpaths: path to capture attributes when used with xpath
    :param excludepaths: paths to exclude
    :param multi: how many files to convert concurrently
    :param no_overwrite: overwrite target file
    :param verbose: stdout log messaging level
    :param log: optional log file
    :param delete_xml: optional delete xml file after converting
    :param xml_files: list of xml_files

    """

    formatter = logging.Formatter("%(levelname)s - %(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.getLevelName(verbose))
    _logger.addHandler(ch)

    if log:
        # create log file handler and set level to debug
        fh = logging.FileHandler(log)
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        _logger.addHandler(fh)

    _logger.info("Parsing XML Files..")

    if target_path:
        if target_path.startswith("hdfs:"):
            if server:
                if subprocess.call(["ssh", server, "hadoop fs -test -e " + target_path]) != 0:
                    _logger.error("invalid target_path: " + target_path + " using hadoop server: " + server)
                    sys.exit(1)
            elif shutil.which("hadoop"):
                if subprocess.call(["hadoop", "fs", "-test", "-e", target_path]) != 0:
                    _logger.error("invalid target_path: " + target_path)
                    sys.exit(1)
            else:
                _logger.error("no hadoop client found")
                sys.exit(1)
        else:
            if not os.path.exists(target_path):
                _logger.error("invalid target_path specified")
                sys.exit(1)

    # open target files
    file_list = list(set([f for _files in [('-' if xml_files[x] == '-' else glob.glob(xml_files[x])) for x in range(0, len(xml_files))] for f in _files]))
    file_count = len(file_list)

    if multi > 1:
        parse_queue_pool = Pool(processes=multi)

    _logger.info("Processing " + str(file_count) + " files")

    if 1 < len(file_list) <= 1000:
        file_list.sort(key=os.path.getsize, reverse=True)
        _logger.info("Parsing files in the following order:")
        _logger.info(file_list)

    for filename in file_list:

        path, xml_file = os.path.split(os.path.realpath(filename))

        if xml_file == '-':
            output_file = '-'

        else:
            output_file = xml_file

            if output_file.endswith(".gz"):
                output_file = output_file[:-3]

            if output_file.endswith(".tar"):
                output_file = output_file[:-4]

            if output_file.endswith(".zip"):
                output_file = output_file[:-4]

            if output_file.endswith(".xml"):
                output_file = output_file[:-4]

            if output_format == "jsonl":
                output_file = output_file + ".jsonl"
            else:
                output_file = output_file + ".json"

            if zip:
                output_file = output_file + ".gz"

            if not target_path:
                output_file = os.path.join(path, output_file)
                if no_overwrite and os.path.isfile(output_file):
                    _logger.debug("No overwrite. Skipping " + xml_file)
                    continue
            elif target_path.startswith("hdfs:"):
                if no_overwrite and subprocess.call(["hadoop", "fs", "-test", "-e", os.path.join(target_path, output_file)]) == 0:
                    _logger.debug("No overwrite. Skipping " + xml_file)
                    continue
                output_file = os.path.join(path, output_file)
            else:
                output_file = os.path.join(target_path, output_file)
                if no_overwrite and os.path.isfile(output_file):
                    _logger.debug("No overwrite. Skipping " + xml_file)
                    continue

        if multi > 1:
            parse_queue_pool.apply_async(parse_file, args=(filename, output_file, xsd_file, output_format, zip, xpath, attribpaths, excludepaths, target_path, server, delete_xml), error_callback=_logger.info)
        else:
            parse_file(filename, output_file, xsd_file, output_format, zip, xpath, attribpaths, excludepaths, target_path, server, delete_xml)

    if multi > 1:
        parse_queue_pool.close()
        parse_queue_pool.join()
