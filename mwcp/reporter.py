"""
    DC3-MWCP framework primary object used for execution of parsers and collection of metadata
"""
from __future__ import print_function

import contextlib

from future.builtins import str

import base64
import hashlib
import json
import ntpath
import os
import pefile
import re
import shutil
import sys
import tempfile
import traceback
from io import BytesIO

import mwcp


PY3 = sys.version_info > (3,)

# pefile is now strictly optional, loaded down below so we can use
# reporter for error reporting

INFO_FIELD_ORDER = ['inputfilename', 'md5', 'sha1', 'sha256', 'compiletime']
STANDARD_FIELD_ORDER = ["c2_url", "c2_socketaddress", "c2_address", "url", "urlpath",
                        "socketaddress", "address", "port", "listenport",
                        "credential", "username", "password",
                        "missionid", "useragent", "interval", "version", "mutex",
                        "service", "servicename", "servicedisplayname", "servicedescription",
                        "serviceimage", "servicedll", "injectionprocess",
                        "filepath", "directory", "filename",
                        "registrykeyvalue", "registrykey", "registryvalue", "key"]


class Reporter(object):
    """
    Class for doing heavy lifting of parser execution and metadata reporting

    This class contains state and data about the current config parsing run, including extracted
    metadata, holding the actual sample, etc.

    Re-using an instance of this class on multiple samples is possible and should be safe, but it
    is not recommended

    Parameters:
        parserdir: sets attribute
        tempdir: sets attribute
        outputdir: sets directory for output_file(). Should not be written to (or read from) by parsers directly (use tempdir)
        outputfile_prefix: sets prefix for output files written to outputdir. Special value "md5" causes prefix by md5 of the input file.
        interpreter_path: overrides value returned by interpreter_path()
        disabledebug: disable inclusion of debug messages in output
        disableoutputfiles: disable writing if files to filesystem
        disabletempcleanup: disable cleanup (deletion) of temp files
        disableautosubfieldparsing: disable parsing of metadata item of subfields
        disablevaluededup: disable deduplication of metadata items
        disablemodulesearch: disable search of modules for parsers, only look in parsers directory

    Attributes:
        parserdir: Optional extra directory where parsers reside.
        tempdir: directory where temporary files should be created. Files created in this directory should
            be deleted by parser. See managed_tempdir for mwcp managed directory
        data: buffer containing input file to parsed
        handle: file handle (BytesIO) of file to parsed
        metadata: Dictionary containing the metadata extracted from the malware by the parser
        pe: a pefile object if pefile successfull parsed the file
        outputfiles: dictionary of entries for each ouput file. The key is the filename specified. Each entry
            is a dictionary with keys of data, description, and md5. If the path key is set, the file was written
            to that path on the filesystem.
        fields: dictionary containing the standardized fields with each field comprising an embedded
            dictionary. The 1st level keys are the field names. Under that, the keys are "description",
            "examples", and "type". See fields.json.
        errors: list of errors generated by framework. Generally parsers should not set these, they should
            use debug instead

    """
    def __init__(self,
                 parserdir=None,
                 outputdir=None,
                 tempdir=None,
                 outputfile_prefix=None,
                 interpreter_path=None,
                 disabledebug=False,
                 disableoutputfiles=False,
                 disabletempcleanup=False,
                 disableautosubfieldparsing=False,
                 disablevaluededup=False,
                 disablemodulesearch=False,
                 base64outputfiles=False,
                 ):

        # defaults
        self.tempdir = tempdir or tempfile.gettempdir()
        self.outputfiles = {}
        self.data = b''
        self.handle = None
        self.fields = {"debug": {"description": "debug", "type": "listofstrings"}}
        self.metadata = {}
        self.errors = []
        self.pe = None

        self.__debug_stdout = None
        self.__orig_stdout = None
        self.__filename = ''
        self.__tempfilename = ''
        self.__managed_tempdir = ''
        self.__outputdir = outputdir or ''
        self.__outputfile_prefix = outputfile_prefix or ''

        # Register parsers from given directory.
        # Only register if a custom parserdir was provided or MWCP's entry_points did not get registered because
        # the project was not installed with setuptools.
        # NOTE: This is all to keep backwards compatibility. mwcp.register_parser_directory() should be
        # called outside of this class in the future.
        default_parserdir = os.path.dirname(mwcp.parsers.__file__)
        self.parserdir = parserdir or default_parserdir
        if self.parserdir != default_parserdir or not list(mwcp.iter_parsers(source='mwcp')):
            mwcp.register_parser_directory(parserdir)

        self.__interpreter_path = interpreter_path
        self.__disabledebug = disabledebug
        self.__disableoutputfiles = disableoutputfiles
        self.__disabletempcleanup = disabletempcleanup
        self.__disableautosubfieldparsing = disableautosubfieldparsing
        self.__disablevaluededup = disablevaluededup
        self.__disablemodulesearch = disablemodulesearch
        self.__base64outputfiles = base64outputfiles

        self.__orig_stdout = sys.stdout

        # TODO: Move fields.json to shared data or config folder.
        fieldspath = os.path.join(os.path.dirname(mwcp.resources.__file__), "fields.json")

        with open(fieldspath, 'rb') as f:
            self.fields = json.load(f)

    def filename(self):
        """
        Returns the filename of the input file. If input was not a filesystem object, we create a
        temp file that is cleaned up after parser is finished (unless tempcleanup is disabled)
        """
        if self.__filename:
            # we were given a filename, give it back
            return self.__filename
        else:
            # we were passed data buffer. Lazy initialize a temp file for this
            if not self.__tempfilename:
                with tempfile.NamedTemporaryFile(delete=False, dir=self.managed_tempdir(), prefix="mwcp-inputfile-") as tfile:
                    tfile.write(self.data)
                    self.__tempfilename = tfile.name

                if self.__disabletempcleanup:
                    self.debug("Using tempfile as input file: %s" %
                               (self.__tempfilename))

            return self.__tempfilename

    def managed_tempdir(self):
        """
        Returns the filename of a managed temporary directory. This directory will be deleted when
        parser is finished, unless tempcleanup is disabled.
        """

        if not self.__managed_tempdir:
            self.__managed_tempdir = tempfile.mkdtemp(
                dir=self.tempdir, prefix="mwcp-managed_tempdir-")

            if self.__disabletempcleanup:
                self.debug("Using managed temp dir: %s" %
                           (self.__managed_tempdir))

        return self.__managed_tempdir

    def interpreter_path(self):
        """
        Returns the path for python interpreter, assuming it can be found. Because of various
        factors (inlcuding ablity to override) this may not be accurate.

        """
        if not self.__interpreter_path:
            # first try sys.executable--this is reliable most of the time but
            # doesn't work when python is embedded, ex. using wsgi mod for web
            # server
            if "python" in os.path.basename(sys.executable):
                self.__interpreter_path = sys.executable
            # second try sys.prefix and common executable names
            else:
                possible_path = os.path.join(sys.prefix, "python.exe")
                if os.path.exists(possible_path):
                    self.__interpreter_path = possible_path
                possible_path = os.path.join(sys.prefix, "bin", "python")
                if os.path.exists(possible_path):
                    self.__interpreter_path = possible_path
            # other options to consider:
            # look at some library paths, such as os.__file__, use system path to find python
            # executable that uses that library use shell and let it find python. Ex. which python
        return self.__interpreter_path

    def error(self, message):
        """
        Record an error message--typically only framework reports error and parsers report via debug
        """
        self.errors.append(message)

    def debug(self, message):
        """
        Record a debug message
        """
        if not self.__disabledebug:
            self.add_metadata("debug", message)

    def __add_metatadata_listofstrings(self, keyu, value):

        try:
            valueu = self.convert_to_unicode(value)
            if keyu not in self.metadata:
                self.metadata[keyu] = []
            if valueu not in self.metadata[keyu] or self.__disablevaluededup or keyu == 'debug':
                self.metadata[keyu].append(valueu)

            if not self.__disableautosubfieldparsing:
                if keyu == "filepath":
                    # use ntpath instead of os.path so we are consistant across platforms. ntpath
                    # should work for both windows and unix paths. os.path works for the platform
                    # you are running on, not necessarily what the malware was written for.
                    # Ex. when running mwcp on linux to process windows
                    # malware, os.path will fail due to not handling
                    # backslashes correctly.
                    self.add_metadata("filename", ntpath.basename(valueu))
                    self.add_metadata("directory", ntpath.dirname(valueu))
                if keyu == "c2_url":
                    self.add_metadata("url", valueu)
                if keyu == "c2_address":
                    self.add_metadata("address", valueu)
                if keyu == "serviceimage":
                    # we use tactic of looking for first .exe in value. This is
                    # not garunteed to be reliable
                    if '.exe' in valueu:
                        self.add_metadata("filepath", valueu[
                                          0:valueu.find('.exe') + 4])
                if keyu == "servicedll":
                    self.add_metadata("filepath", valueu)
                if keyu == "url" or keyu == "c2_url":
                    # http://[fe80::20c:1234:5678:9abc]:80/badness
                    # http://bad.com:80
                    # ftp://127.0.0.1/really/bad?hostname=pwned
                    match = re.search(
                        r"[a-z\.-]{1,40}://(\[?[^/]+\]?)(/[^?]+)?", valueu)
                    if match:
                        if match.group(1):
                            address = match.group(1)
                            if address[0] == "[":
                                # ipv6--something like
                                # [fe80::20c:1234:5678:9abc]:80
                                parts = address.split("]")
                                if len(parts) > 1:
                                    if parts[1]:
                                        if keyu == "c2_url":
                                            self.add_metadata("c2_socketaddress", [
                                                              parts[0][1:], parts[1][1:], "tcp"])
                                        else:
                                            self.add_metadata("socketaddress", [
                                                              parts[0][1:], parts[1][1:], "tcp"])
                                else:
                                    if keyu == "c2_url":
                                        self.add_metadata(
                                            "c2_address", parts[0][1:])
                                    else:
                                        self.add_metadata(
                                            "address", parts[0][1:])
                            else:
                                # regular domain or ipv4--bad.com:80 or
                                # 127.0.0.1
                                parts = address.split(":")
                                if len(parts) > 1:
                                    if parts[1]:
                                        if keyu == "c2_url":
                                            self.add_metadata("c2_socketaddress", [
                                                              parts[0], parts[1], "tcp"])
                                        else:
                                            self.add_metadata("socketaddress", [
                                                              parts[0], parts[1], "tcp"])
                                else:
                                    if keyu == "c2_url":
                                        self.add_metadata(
                                            "c2_address", parts[0])
                                    else:
                                        self.add_metadata("address", parts[0])
                        if match.group(2):
                            self.add_metadata("urlpath", match.group(2))
                    else:
                        self.debug("Error parsing as url: %s" % valueu)

        except Exception:
            self.debug("Error adding metadata for key: %s\n%s" %
                       (keyu, traceback.format_exc()))

    def __add_metadata_listofstringtuples(self, keyu, value):
        try:
            values = []
            if not value:
                self.debug("no values provided for %s, skipping" % keyu)
                return
            for thisvalue in value:
                values.append(self.convert_to_unicode(thisvalue))

            if keyu not in self.metadata:
                self.metadata[keyu] = []
            if self.__disablevaluededup:
                self.metadata[keyu].append(values)
            else:
                try:
                    dedupindex = self.metadata[keyu].index(values)
                except ValueError:
                    self.metadata[keyu].append(values)

            if not self.__disableautosubfieldparsing:
                # TODO: validate lengths for known types
                if keyu == "c2_socketaddress":
                    self.add_metadata("socketaddress", values)
                    self.add_metadata("c2_address", values[0])
                elif keyu == "socketaddress":
                    self.add_metadata("address", values[0])
                    if len(values) >= 3:
                        self.add_metadata("port", [values[1], values[2]])
                    if len(values) != 3:
                        self.debug(
                            "Expected three values in type socketaddress, received %i" % len(values))
                elif keyu == "credential":
                    self.add_metadata("username", values[0])
                    if len(values) >= 2:
                        self.add_metadata("password", values[1])
                    if len(values) != 2:
                        self.debug(
                            "Expected two values in type credential, received %i" % len(values))
                elif keyu == "port" or keyu == "listenport":
                    if len(values) != 2:
                        self.debug("Expected two values in type %s, received %i" % (
                            keyu, len(values)))
                    # check for integer number and valid proto?
                    match = re.search(r"[0-9]{1,5}", values[0])
                    if match:
                        portnum = int(values[0])
                        if portnum < 0 or portnum > 65535:
                            self.debug(
                                "Expected port to be number between 0 and 65535")
                    else:
                        self.debug(
                            "Expected port to be number between 0 and 65535")
                    if len(values) >= 2:
                        if values[1] not in ["tcp", "udp", "icmp"]:
                            self.debug(
                                "Expected port type to be tcp or udp (or icmp)")
                elif keyu == "registrypathdata":
                    self.add_metadata("registrypath", values[0])
                    if len(values) >= 2:
                        self.add_metadata("registrydata", values[1])
                    if len(values) != 2:
                        self.debug(
                            "Expected two values in type registrypathdata, received %i" % len(values))
                elif keyu == "service":
                    if values[0]:
                        self.add_metadata("servicename", values[0])
                    if len(values) >= 2:
                        if values[1]:
                            self.add_metadata("servicedisplayname", values[1])
                    if len(values) >= 3:
                        if values[2]:
                            self.add_metadata("servicedescription", values[2])
                    if len(values) >= 4:
                        if values[3]:
                            self.add_metadata("serviceimage", values[3])
                    if len(values) >= 5:
                        if values[4]:
                            self.add_metadata("servicedll", values[4])

                    if len(values) != 5:
                        self.debug(
                            "Expected 5 values in type service, received %i" % len(values))

        except Exception:
            self.debug("Error adding metadata for key: %s\n%s" %
                       (keyu, traceback.format_exc()))

    def __add_metadata_dictofstrings(self, keyu, value):
        try:
            # check for type of other?
            for thiskey in value:
                if isinstance(value[thiskey], str):
                    thiskeyu = self.convert_to_unicode(thiskey)
                    thisvalueu = self.convert_to_unicode(value[thiskey])
                    if 'other' not in self.metadata:
                        self.metadata['other'] = {}
                    if thiskeyu in self.metadata['other']:
                        # this key already exists, we don't want to clobber so
                        # we turn into list?
                        existingvalue = self.metadata['other'][thiskeyu]
                        if isinstance(existingvalue, list):
                            if thisvalueu not in self.metadata['other'][thiskeyu]:
                                self.metadata['other'][
                                    thiskeyu].append(thisvalueu)
                        else:
                            if thisvalueu != existingvalue:
                                self.metadata['other'][thiskeyu] = [
                                    existingvalue, thisvalueu]
                    else:
                        # normal insert of single value
                        self.metadata['other'][thiskeyu] = thisvalueu
                else:
                    # TODO: support inserts of lists (assuming members are
                    # strings)?
                    self.debug("Could not add object of %s to metadata under other using key %s" % (
                        str(type(value[thiskey])), thiskey))
        except Exception:
            self.debug("Error adding metadata for key: %s\n%s" %
                       (keyu, traceback.format_exc()))

    def add_metadata(self, key, value):
        """
        Report a metadata item

        Primary method to report metadata as a result of parsing.

        Args:
            key: string specifying the key of the metadata. Should be one of values specified in fields.json.
            value: string specifying the value of the metadata. Should be a utf-8 encoded string or a unicode object.

        """

        try:
            keyu = self.convert_to_unicode(key)
        except Exception:
            self.debug("Error adding metadata due to failure converting key to unicode: %s" % (
                traceback.format_exc()))
            return

        if keyu in self.fields:
            fieldtype = self.fields[keyu]['type']
        else:
            self.debug(
                "Error adding metadata because %s is not an allowed key" % (keyu))
            return

        if fieldtype == "listofstrings":
            self.__add_metatadata_listofstrings(keyu, value)

        if fieldtype == "listofstringtuples":
            self.__add_metadata_listofstringtuples(keyu, value)

        if fieldtype == "dictofstrings":
            self.__add_metadata_dictofstrings(keyu, value)

    def convert_to_unicode(self, input_string):
        if isinstance(input_string, str):
            return input_string
        else:
            return str(input_string, encoding='utf8', errors='replace')

    def run_parser(self, name, filename=None, data=b"", **kwargs):
        """
        Runs specified parser on file

        :param str name: name of parser module to run (use ":" notation to specify source if necessary e.g. "mwcp-acme:Foo")
        :param str filename: file to parse
        :param bytes data: use data as file instead of loading data from filename
        """
        self.__reset()

        if filename:
            self.__filename = filename
            with open(self.__filename, 'rb') as f:
                self.data = f.read()
        else:
            self.data = data

        self.handle = BytesIO(self.data)

        if self.data[:2] == b"MZ":
            # We create pefile object from input file if we can
            # We want to be able to catch import error and log it using
            # reporter object.
            try:
                self.pe = pefile.PE(data=self.data)
            except Exception as e:
                self.debug("Error parsing with pefile: %s" % (str(e)))

        try:
            with self.__redirect_stdout():
                found = False
                for parser_name, source, parser_class in mwcp.iter_parsers(name):
                    found = True
                    self.debug('[*] Running parser: {}:{}'.format(source, parser_name))
                    self.handle.seek(0)
                    try:
                        parser = parser_class(reporter=self)
                        parser.run(**kwargs)
                    except (Exception, SystemExit) as e:
                        if filename:
                            identifier = filename
                        else:
                            identifier = hashlib.md5(data).hexdigest()
                        self.error("Error running parser {}:{} on {}: {}".format(
                            source, parser_name, identifier, traceback.format_exc()))

                if not found:
                    self.error('Could not find parsers with name: {}'.format(name))
        finally:
            self.__cleanup()

    def pprint(self, data):
        """
        JSON Pretty Print data
        """
        return json.dumps(data, indent=4)

    def output_file(self, data, filename, description=''):
        """
        Report a file created by the parser

        This should involve a file created by the parser and related to the malware.

        :param bytes data: The contents of the output file
        :param str filename: filename (basename) of file
        :param str description: description of the file
        """
        basename = os.path.basename(filename)
        md5 = hashlib.md5(data).hexdigest()
        self.outputfiles[filename] = {
            'data': data, 'description': description, 'md5': md5}

        if self.__base64outputfiles:
            self.add_metadata(
                "outputfile", [basename, description, md5, base64.b64encode(data)])
        else:
            self.add_metadata("outputfile", [basename, description, md5])

        if self.__disableoutputfiles:
            return

        if self.__outputfile_prefix:
            if self.__outputfile_prefix == "md5":
                fullpath = os.path.join(self.__outputdir, "%s_%s" % (
                    hashlib.md5(self.data).hexdigest(), basename))
            else:
                fullpath = os.path.join(self.__outputdir, "%s_%s" % (
                    self.__outputfile_prefix, basename))
        else:
            fullpath = os.path.join(self.__outputdir, basename)

        try:
            with open(fullpath, "wb") as f:
                f.write(data)
            self.debug("outputfile: %s" % (fullpath))
            self.outputfiles[filename]['path'] = fullpath
        except Exception as e:
            self.debug("Failed to write output file: %s, %s" %
                       (fullpath, str(e)))

    def report_tempfile(self, filename, description=''):
        """
        load filename from filesystem and report using output_file
        """
        if os.path.isfile(filename):
            with open(filename, "rb") as f:
                data = f.read()
            self.output_file(data, os.path.basename(filename), description)
        else:
            self.debug(
                "Could not output file because it could not be found: %s" % (filename))

    def format_list(self, values, key=None):

        if key == "credential" and len(values) == 2:
            return "%s:%s" % (values[0], values[1])
        elif key == "outputfile" and len(values) >= 3:
            return "%s %s" % (values[0], values[1], values[2])
        elif key == "port" and len(values) == 2:
            return "%s/%s" % (values[0], values[1])
        elif key == "listenport" and len(values) == 2:
            return "%s/%s" % (values[0], values[1])
        elif key == "registrykeyvalue" and len(values) == 2:
            return "%s=%s" % (values[0], values[1])
        elif key == "socketaddress" and len(values) == 3:
            return "%s:%s/%s" % (values[0], values[1], values[2])
        elif key == "c2_socketaddress" and len(values) == 3:
            return "%s:%s/%s" % (values[0], values[1], values[2])
        elif key == "service" and len(values) == 5:
            return "%s, %s, %s, %s, %s" % (values[0], values[1], values[2], values[3], values[4])
        else:
            return ' '.join(values)

    def print_keyvalue(self, key, value):
        print(self.get_printable_key_value(key, value))

    def output_text(self):
        """
        Output in human readable report format
        """

        output = self.get_output_text()
        print(output)

    def get_printable_key_value(self, key, value):
        output = ""
        printkey = key

        if isinstance(value, str):
            output += "{:20} {}\n".format(printkey, value)
        else:
            for item in value:
                if isinstance(item, str):
                    output += "{:20} {}\n".format(printkey, item)
                else:
                    output += "{:20} {}\n".format(printkey,
                                                   self.format_list(item, key=key))
                printkey = ""

        return output

    def get_output_text(self):
        """
        Get data in human readable report format.
        """

        output = ""
        infoorderlist = INFO_FIELD_ORDER
        fieldorderlist = STANDARD_FIELD_ORDER

        if 'inputfilename' in self.metadata:
            output += "\n----File Information----\n\n"
            for key in infoorderlist:
                if key in self.metadata:
                    output += self.get_printable_key_value(
                        key, self.metadata[key])

        output += "\n----Standard Metadata----\n\n"

        for key in fieldorderlist:
            if key in self.metadata:
                output += self.get_printable_key_value(key, self.metadata[key])

        # in case we have additional fields in fields.json but the order is not
        # updated
        for key in self.metadata:
            if key not in fieldorderlist and key not in ["other", "debug", "outputfile"] and key in self.fields:
                output += self.get_printable_key_value(key, self.metadata[key])

        if "other" in self.metadata:
            output += "\n----Other Metadata----\n\n"
            for key in sorted(list(self.metadata["other"])):
                output += self.get_printable_key_value(
                    key, self.metadata["other"][key])

        if "debug" in self.metadata:
            output += "\n----Debug----\n\n"
            for item in self.metadata["debug"]:
                output += "{}\n".format(item)

        if "outputfile" in self.metadata:
            output += "\n----Output Files----\n\n"
            for value in self.metadata["outputfile"]:
                output += self.get_printable_key_value(
                    value[0], (value[1], value[2]))

        if self.errors:
            output += "\n----Errors----\n\n"
            for item in self.errors:
                output += "{}\n".format(item)

        return output

    @contextlib.contextmanager
    def __redirect_stdout(self):
        """Redirects stdout temporarily to self.debug."""
        debug_stdout = BytesIO()
        orig_stdout = sys.stdout
        sys.stdout = debug_stdout
        try:
            yield
        finally:
            if not self.__disabledebug:
                for line in debug_stdout.getvalue().splitlines():
                    self.debug(line)
            sys.stdout = orig_stdout

    def __reset(self):
        """
        Reset all the data in the reporter object that is set during the run_parser function

        Goal is to make the reporter safe to use for multiple run_parser instances
        """
        self.__filename = ''
        self.__tempfilename = ''
        self.__managed_tempdir = ''

        self.data = b''
        self.handle = None

        self.metadata = {}
        self.outputfiles = {}
        self.errors = []
        self.pe = None

    def __cleanup(self):
        """
        Cleanup things
        """
        if not self.__disabletempcleanup:
            if self.__tempfilename:
                try:
                    os.remove(self.__tempfilename)
                except Exception as e:
                    self.debug("Failed to purge temp file: %s, %s" %
                               (self.__tempfilename, str(e)))
                self.__tempfilename = ''
            if self.__managed_tempdir:
                try:
                    shutil.rmtree(self.__managed_tempdir, ignore_errors=True)
                except Exception as e:
                    self.debug("Failed to purge temp dir: %s, %s" %
                               (self.__managed_tempdir, str(e)))
                self.__managed_tempdir = ''

        self.__tempfilename = ''
        self.__managed_tempdir = ''

    def __del__(self):
        self.__cleanup()
