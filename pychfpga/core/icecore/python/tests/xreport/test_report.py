import lxml.etree
import lxml.objectify
import lxml.html
from copy import deepcopy
import matplotlib.pyplot as plt
# import numpy as np
import base64
import urllib
import datetime
import os
import glob
import logging
from StringIO import StringIO
import pickle
import bz2
import re
import tempfile
import shutil


from rst import rST
from namespace import NameSpace

#------------------------------------------------
# XML nodes
#------------------------------------------------
# These nodes are used
# "group", "case" store a herarchy of test cases.
GROUP = lxml.objectify.E.group  # Contains all data from a nose 'context' (package, module, class, generator)
CASE = lxml.objectify.E.case # Contains all data from a nose 'testcase' (function, method)

CONTEXT = lxml.objectify.E.context  # Wraps a number of ITEMs that describe context information on the test. The ITEM elements are meant to be converted into a table.
ITEM = lxml.objectify.E.item # Represents a table row. Contains a NAME and a VALUE node.
NAME = lxml.objectify.E.name
VALUE = lxml.objectify.E.value


# Both GROUP and CASE can have the following children
TITLE = lxml.objectify.E.title  # Name of the test shown as header and in summary tables
DESCRIPTION = lxml.objectify.E.description  # Description of the test, taken from the docstring
DETAILS = lxml.objectify.E.details # test results (captured output and error messages)
PASSED = lxml.objectify.E.passed  # Pass/fail status of the current GROUP or CASE
SUMMARY = lxml.objectify.E.summary # Pass/fail short informative message, shown in summary tables
TESTNAME = lxml.objectify.E.testname  # Current test name
TESTPATH = lxml.objectify.E.testpath  # Hierarchical test name
TESTDATE = lxml.objectify.E.testdate  # Current test date & time
TESTARGS = lxml.objectify.E.testargs  # Current test arguments
SYNOPSIS_DATA = lxml.objectify.E.synopsis_data
HEADER = lxml.objectify.E.header  # Additional headers to separate test result sections

#  HTML elements that can be useful for styling "description", "details", and
#  "summary"
P = lxml.objectify.E.p  # Paragraph
A = lxml.objectify.E.A  # Hyperlink
B = lxml.objectify.E.b  # boldface
CODE = lxml.objectify.E.code
PRE = lxml.objectify.E.pre  # preformatted code (keep spaces and newlines)
FIGURE = lxml.objectify.E.figure  # Contains IMG and FIGCAPTION nodes
IMG = lxml.objectify.E.img  # Image
FIGCAPTION = lxml.objectify.E.figcaption
DATA = lxml.objectify.E.data  # Raw data



"""
XML Schema
<group> # Test run
    <group> # Context: Module/package
        <testname>
        <testpath>
        <testdate>
        <title>
        <description>
            <details>, <details> ...
        <passed>
        <context>
        <case> # function
            <testname>
            <testpath>
            <testdate>
            <title>
            <description>
                <details>, <details> ...
            <passed>
            <details> or <figure>
            ...
            <details> or <figure>
        ...
        <case>
            ...
        <group> # Context: generator or class
            ...
    <details> # Final pass/fail results
"""

class TestReport(object):
    """ Object containing both the text and raw data generated during testing
    in a way that allows the results to be easily queried, summarized and converted
    into HTML, resctructred text and PDF reports.

    The TestReport can be saved as an XML file that contains both the text,
    images, plots and raw data generated during the test.The XML file embeds
    an XSLT template that make it directly viewable by browsers as a
    structured report just by opening it.

    The native reporting format is reStructuredText, which can be converted to any other
    format supporteed by the docutils library.

    PDF documents can also be generated directly (without the need of LaTex) by using the ``rst2pdf`` package.

    The test results can be structured hierarchically in any number of 'group'
    objects which can contain any number of other groups objects or test
    results objects ('case' objects).

    Groups and Cases are created and made the new default node by the
    enter_group_node() or enter_case_node() methods. exit_node() returns the
    current node to the previous level.

    A top level group is created automatically when the TestReport object is instantiated.

    Each group and test case must be provided basic test information using
    add_group_info(). The information include:
        - Title to be displayed in reports (e.g. 'METHOD my test')
        - Name of the node (e.g. my_test)
        - Full path of the node from the root (e.g. my_module.my_class.my_test)
        - Date and time of creation (in ISO format)

    Each group and test case must also contain pass/fail status using the
    add_passed_node() method.

    Text and plots can be added to the current node with
    add_literal_text_block(), add_text_block() and add_plot() method.


    """
    def __init__(self, filename=None, formats=['pdf']):
        super(TestReport, self).__init__()
        self.logger = logging.getLogger('')

        self.etree = GROUP()
        self.node_tail = [self.etree]
        self.node_name = []


        if filename:
            filename, filename_ext = os.path.splitext(filename)
            if filename_ext[1:]:
                formats.add(filename_ext[1:])

        for f in formats:
            if f not in ('pdf', 'rst', 'xml'):
                raise ValueError("Invalid file format type '%s'" % f)

        self.filename = filename
        self.formats = formats

        self.transtable = unicode(''.join(chr(i) if (32 <= i <= 127) or i == 10 or i == 13 else '.' for i in range(256)))

    ##################################################
    # Node management
    ##################################################

    def get_current_node(self):
        """ Return the current XML node """
        return self.node_tail[-1]


    def add_node(self, *data):
        """ Add one or more children XML elements to the current node"""
        if not self.node_tail:
            print 'add_node: There is no tail node!'
            return
        self.get_current_node().extend(data)

    def enter_node(self, node, node_name):
        """ Create a new XML children node and make it current. ``node`` name
        is added to the current name path
        """
        self.add_node(node)
        self.node_tail.append(node)  # make new node current
        self.node_name.append(node_name)

    def enter_group_node(self, node_name):
        self.enter_node(node=GROUP(), node_name=node_name)

    def enter_case_node(self, node_name):
        self.enter_node(node=CASE(), node_name=node_name)

    def exit_node(self):
        """ Terminate a node and make parent node current"""
        self.node_tail.pop()
        if self.node_name:
            self.node_name.pop()  # flush the current node name

    def get_node_path(self):
        """ Return the full hierarchical name of the current node"""
        return '.'.join(self.node_name)

    def add_context_table(self, table):
        """ Add a context table. Each item of the list is a 2 element list
        corresponding to the header and value of each row.
        """
        self.add_node(
            CONTEXT(*[ITEM(NAME(k), VALUE(v)) for (k, v) in table]))

    def add_literal_text_block(self, text):
        self.add_node(DETAILS(PRE(text.translate(self.transtable))))


    def add_text_block(self, text):
        """ Add a block of RestructuredText
        """
        self.add_node(DETAILS(P(text)))

    def add_header(self, text):
        """ Adds a subheader to the current node. All subheaders are at the
        same level. This is purely decorative and does not affect the
        hierarchy level of the data.
        """
        self.add_node(HEADER(text))

    def add_summary(self, text):
        """ Adds a summary of the tests.
        """
        self.add_node(SUMMARY(text))

    def add_plot(self, caption='', format='png'):
        """ Insert a matplotlib plot in the test report"""
        # Grab and convert image to URI
        io = StringIO()
        plt.gcf().savefig(io, format=format)
        self.last_image = io.getvalue()

        uri = ('data:image/%s;base64,' % format +
               urllib.quote(base64.b64encode(io.getvalue())))
        # insert image
        self.add_node(FIGURE(IMG(src=uri), FIGCAPTION(caption), style='text-align:center'))

    def add_passed_node(self, passed):
        self.add_node(PASSED(passed))

    def add_group_info(self, test_name='', title='',  test_path=None, test_date=None, test_args=None, short_description=None, long_description=None):
        if test_path is None:
            test_path = self.get_node_path()
        if test_date is None:
            test_date = datetime.datetime.now().isoformat()
        # self.rep.add_node(TESTNAME(sys.argv[0]))
        self.add_node(TITLE(title))
        self.add_node(TESTNAME(test_name))
        self.add_node(TESTPATH(test_path))
        self.add_node(TESTDATE(test_date))
        if test_args:
            self.add_node(TESTARGS(test_args))
        description = []
        if short_description:
            description.append(B(short_description))
        if long_description:
            description.append(DETAILS(long_description))
        if description:
            self.add_node(DESCRIPTION(*description))

    def add_data(self, data):
        self.add_node(DATA(self._encode_data(data)))


    ##################################################
    # queries
    ##################################################

    def get_nodes(self, node='*', required_field=None):
        node = node.replace(':', '.')
        for char in "\\^[].${}(+()!":  # escape regex special characters. \\ must be first
            node = node.replace(char, '\\'+ char)
        node = '^' + node.replace('*', '.*').replace('?','.') + '$'  # add end delimiters and translate wildcards
        # if not node:
        #     node = self.get_node_path()
        # if node.startswith('.'):
        #     prefix_nodes = self.node_name[:]
        #     node = node[1:]
        #     while node.startswith('.'):
        #         print prefix_nodes
        #         node = node[1:]
        #         prefix_nodes.pop()
        #     node = '.'.join(prefix_nodes) + '.' + node
        # print 'searching for', node
        return [ n for n in self.etree.iter('group', 'case') if re.match(node, str(n.testpath)) and (not required_field or hasattr(n, required_field))]

    def get_field(self, field, path='*', single=False, func=lambda x:x):
        nodes = self.get_nodes(path, required_field=field)
        if single:
            if len(nodes) == 0:
                raise RuntimeError('Cannot find group or test case under path %s' % path)
            if len(nodes) > 1:
                raise RuntimeError('Multiple nodes matched the path %s' % path)
            return func(getattr(nodes[0], field))
        else:
            return [func(getattr(node, field)) for node in nodes]

    DATA_ENCODING_FORMATS = [
        # format, encode fn, decode fn

        # Unfortunately, piclke protocol 0 can return non-ascii characters if it encounters unicode strings, which lxml doesn't like
        # The community consensus is to use base64 around it. So we might as well use the highest pickle protocol (proto number -1) and compress the data
        ('pickle+bz2+base64', lambda obj: base64.encodestring(bz2.compress(pickle.dumps(obj, -1))), lambda obj: pickle.loads(bz2.decompress(base64.decodestring(obj))) )
        ]

    def _encode_data(self, obj):
        # By default encode with the first protocol on the list
        fmt, _encode_fn, _ = self.DATA_ENCODING_FORMATS[0]
        return fmt + ':' + _encode_fn(obj)

    def _decode_data(self, obj):
        s = str(obj)
        for fmt, _, _decode_fn in self.DATA_ENCODING_FORMATS:
            if s.startswith(fmt + ':'):
                return _decode_fn(s[len(fmt) + 1:])
        return repr(obj) # fallback: return string representation of the object

    def get_data(self, path='*', single=False):
        return self.get_field('data', path=path, single=single, func=self._decode_data)

    def get_passed(self, path='*', single=False):
        return self.get_field('passed', path=path, single=single)


    def get_data_tree(self):
        """ Return all data in the report as a hierarchy of attributes based on the test name elements.

        Example to get data from test a_module:a_class.a_method
            tree = my_report.get_data_tree()
            my_data = tree.a_module.a_class.a_method

        See also get_data() which can get data from one or multiple tests based on a search pattern.
        """
        nodes = self.get_nodes(required_field='data')
        data_list = [(str(n.testpath), self._decode_data(n.data)) for n in nodes]
        # print data_list
        data = NameSpace()
        for path, value in data_list:
            obj = data
            names = path.split('.')
            for name in names[:-1]:
                if not hasattr(obj, name):
                    setattr(obj, name, NameSpace())
                obj = getattr(obj, name)
            setattr(obj, names[-1], value)
        return data



    def print_etree(self, node=None, level=0):
        if node is None:
            node = self.etree
        self._print_etree(node)

    @classmethod
    def _print_etree(cls, node, level=0):
        """ Print the XML tree in a compact manner.
        The top element is not displayed.
        """
        for e in node:
            if e.text:
                text = str(e.text).replace('\n', ' ')
                if len(text) > 64:
                    text = text[:64] + '...'
            else:
                text = ''
            print '%s%s = %s' % ('   '*level, e.tag.upper(), text)
            cls._print_etree(e.getchildren(), level+1)

    def get_synopsis(self):
        etree = self.etree
        return [(str(getattr(x, 'testdate', '?')),
                 str(getattr(x, 'testpath', '?')),
                 str(getattr(x, 'passed', '?')),
                 str(getattr(x, 'summary', '')))
                for x in etree.iter(['case', 'group'])]

    def get_synopsis_as_strings(self):
        syn = self.get_synopsis()
        col_width = [0, 0, 0, 0]
        for item in syn:
            for (i, field) in enumerate(item):
                    col_width[i] = max(col_width[i], len(str(field)))
        format_ = '| ' + ' | '.join('%%-%is' % width for width in col_width) + ' |'

        return [format_ % item for item in syn]

    def print_synopsis(self):
        print self.get_synopsis_as_strings()

    def get_xml(self, xslt='xreport.xsl'):
        '''Export as XML, with embedded XSL stylesheet so the browser can show the formatted data'''
        if self.etree is None:
            raise RuntimeError('The test has not been run yet')

        # get the XSL style sheet
        stylesheet = os.sep.join(__file__.split(os.sep)[:-1]+[xslt])

        xsl = lxml.etree.parse(open(stylesheet))

        # Create an empty XML document
        xml = lxml.etree.XML(
            '<?xml version="1.0" encoding="UTF-8"?> \n'
            '<!DOCTYPE root [<!ATTLIST xsl:stylesheet id ID  #REQUIRED>]> \n'
            '<?xml-stylesheet type="text/xsl" href="#xslt"?>\n'
            '<root></root>\n')
        xml = lxml.etree.ElementTree(xml)
        root = xml.getroot()  # Get the root object of the empty document
        root.append(xsl.getroot())  # Add the XSL stylesheet to root

        # Add the data to root
        root.append(deepcopy(self.etree)) #use deepcopy because this seems to change the original object

        return lxml.etree.tostring(xml, pretty_print=True)

    def write_xml(self, filename=None):
        filename = filename or self.filename
        # print 'writing XML file in', filename + '.xml'
        with open(filename + '.xml', 'w') as f:
            f.write(self.get_xml())
        # print 'wrote XML file in', filename + '.xml'

    def get_rst(self, filename=None):
        """ Return the object representating the test report in reStructuredTest with attachments."""
        filename = filename or self.filename
        filename, filename_ext = os.path.splitext(filename)

        rst = rST(filename)
        rst.add_toc(depth=5)
        self._get_rst_from_group_node(rst, self.etree)
        return rst

    def _get_rst_from_group_node(self, rst, node, level=0):
        rst.add_section(str(node.title), level)
        # First include the contents of the 'description' (from docstrings).
        # We assume this is always in reStructuredtext format
        for n in node.iterchildren('description'):
            rst.add('\n'.join(n.itertext()))
            rst.add('\n')
        # Insert summary here

        # Now process the test outputs: details and figures, in the order they
        # are stored in the test data structure
        for n in node.iterchildren('details', 'figure', 'header'):
            if n.tag == 'header':
                # self.logger.debug('Got a header on node %s' % (n.tag))
                rst.add_section(str(n), level+1)

            if n.tag == 'figure':
                image = self._get_binary_from_img_node(n.img)
                caption = n.figcaption.text
                rst.add_binary_image(image, caption=caption)
            if n.tag == 'details':
                for nn in n.iterdescendants():
                    # self.logger.debug('Processing node %s.%s' % (n.tag, nn.tag))
                    if nn.tag == 'pre':
                        rst.add_literal_block('\n'.join(nn.itertext()))
                    else:
                        rst.add(nn.text + '\n')
            rst.add('\n')
        for n in node.iterchildren('group', 'case'):
            self._get_rst_from_group_node(rst, n, level+1)

    def _get_binary_from_img_node(self, node):
            target ='base64,'
            src = node.attrib['src']
            start_index = src.find(target) + len(target)
            binary = base64.b64decode(urllib.unquote(src[start_index:]))
            return binary

    def write_rst(self, filename=None):
        filename = filename or self.filename
        rst = self.get_rst(filename)
        rst.write()

    def write_pdf(self, filename=None, log_level=logging.WARNING, image_path=None):
        """ Write the report as a PDF file.

        Latex is not needed. The rst2pdf package (pip install rst2pdf) is required.
        """
        filename = filename or self.filename
        if not filename.lower().endswith('.pdf'):
            filename += '.pdf'
        from reportlab.platypus import flowables
        from rst2pdf import createpdf

        # Set listWrapOnFakeWidth to False to make sure the wrap() command
        # report the actual width of (literal) boxes, not the available width.
        # This way the boxes can scale correctly to fit the width of the page.

        # Note that reportlab's flowables module loads listWrapOnFakeWidth
        # from the rl_config module with an import statement at module load
        # time. Changing the rl_config value after that has no effect. For
        # this reason, we change it firectly in the flowables module.
        flowables.listWrapOnFakeWidth = 0

        # Make sure image files are closed after they are imported so we can delete the temporary folder
        # see https://groups.google.com/forum/#!topic/rst2pdf-discuss/zK5dSiaS7Fo
        import reportlab.rl_config
        reportlab.rl_config.imageReaderFlags = 2

        logger = logging.getLogger('rst2pdf')
        logger.setLevel(log_level)

        attachment_folder = tempfile.mkdtemp()
        rst = self.get_rst(filename)
        rst.write_attachments(attachment_folder)

        # if image_path is None:
        #     image_path = os.path.split(filename)[0]
        r = createpdf.RstToPdf(stylesheets=['eightpoint', 'letter', 'sphinx'], fit_mode='shrink', breaklevel=0, basedir=attachment_folder)
        r.createPdf(text=str(rst), output=filename)
        shutil.rmtree(attachment_folder)

    def publish(self, filename, writer_name='html'):
        """ Write the report in any of the formats supported by the ``docutils`` library.
        """
        filename = filename or self.filename
        rst = self.get_rst(filename)
        rst.write(writer_name=writer_name)

    def write(self, filename=None, formats=None):
        filename = filename or self.filename
        formats = formats or self.formats

        if filename:
            filename, filename_ext = os.path.splitext(filename)
            if filename_ext[1:]:
                formats.append(filename_ext[1:])

        for f in formats:
            if f not in ('pdf', 'rst', 'xml'):
                raise ValueError("Invalid file format type '%s'" % f)

        self.write_xml(filename=filename)
        if 'pdf' in formats:
            self.write_pdf(filename=filename)
        if 'rst' in formats:
            self.write_rst(filename=filename)



# # def load_html(filename):
# #     with open(filename) as file_:
# #         f = file_.read()
# #     return lxml.objectify.fromstring(f)


#     @staticmethod
#     def load_xml(filename):
#         with open(filename) as file_:
#             t = lxml.objectify.parse(file_)
#         return t


# # def get_synopsis_from_xml(filename):
# #     e = load_html(filename)
# #     return get_synopsis(e)

# # def get_synopsis(etree):
# #     return [(str(x.testdate), str(x.testpath), str(x.passed), str(x.summary) if hasattr(x, 'summary') else '') for x in etree.iter(['case', 'group'])]

def load_test_cases(input_folder='.'):
    """ Return a dictionary of test cases found in all the the files in the folder specified by ``input_folder``.

    Each entry is {test_name : [(filename, date, case_object), ...]}. The list elements are sorted by most recent test first.
    """
    logger = logging.getLogger('')
    filenames = glob.glob(input_folder + '/*.xml')

    # Load data from all files
    file_data = {}
    for filename in filenames:
        with open(filename) as file_:
            etree = lxml.objectify.parse(file_)  # use objectify (instead of etree) to allow attribute-wise access to elements
        # print filename
        # print '-' * len(filename)
        file_data[filename] = etree.find('group')


    # build a dictionary of all test cases
    test_cases = {}
    for fn, group in file_data.items():
        for case in group.iter('case'):  # fild all test case objects, wherever they are in the hierarchy
            path = case.testpath
            date = case.testdate
            if path not in test_cases:
                test_cases[path] = []
            test_cases[path].append((fn, date, case))

    # sort the cases by date
    for case_name, case in test_cases.items():
        case.sort(key=lambda c: c[1], reverse=True)  # sort by reverse date

    return test_cases

def get_test_status(test_cases, required_tests):
    """ Return a list indicating whether the specified tests have passed, failed, or were not run yet.

    ``required_tests`` is a list of test_names. Semicolons separators are replaced by '.'.

    The returned value is a list of dictionaries containing the following elements: test_path, test_name, test_date, passed, message.
    """
    required_tests = [t.replace(':', '.') for t in required_tests]

    test_info = []
    for path in required_tests:
        if path in test_cases.keys():
            filename, date, case = test_cases[path][0]  # get the most recent test
            name = getattr(case, 'testname', '')
            passed = getattr(case, 'passed', False)
            message = getattr(case, 'summary', '')
        else:
            filename = None
            name = path.split('.')[-1]
            date = None
            passed = False
            message = 'Test not run'
        test_info.append(NameSpace(
            filename=filename,
            path=path,
            name=name,
            date=date,
            passed=passed,
            message=message))
    return test_info

def generate_test_summary(input_folder='.', required_tests=[], output_filename=None, output_formats=['pdf'], title='Test Summary Report'):
    """ Load all XML test reports in the specified folder and generate a summary report that retains the latest result for every test.

    ``required_test`` is a list of paths describing which tests are expected,
    and in which order. A missing test is considered to be failed. paths are
    typically in the format 'module_name:class_name.method_name'.

    if ``output_filename`` is specified, the summary report is in the format
    determined by the extension. The native report is also always saved with
    the same name but with the .xml extension.

    """
    logger = logging.getLogger('')
    logger.info('start generate test summary')

    test_cases = load_test_cases(input_folder=input_folder)

    def get_case_summary(case_info):
        filename, date, case = case_info
        return (str(getattr(case, 'testdate', '?')),
                 str(getattr(case, 'testpath', '?')),
                 str(getattr(case, 'passed', '?')),
                 str(getattr(case, 'summary', '')))

    def format_summary(syn):
        if not syn:
            return []
        col_width = [0] * len(syn[0])
        for item in syn:
            for (i, field) in enumerate(item):
                    col_width[i] = max(col_width[i], len(str(field)))
        format_ = '| ' + ' | '.join('%%-%is' % width for width in col_width) + ' |'
        return [format_ % item for item in syn]

    def print_summary(summary):
        print '\n'.join(format_summary(summary))


    test_date = datetime.datetime.now().isoformat()
    report = TestReport()  ### debug
    report.add_context_table([
        ['Report date/time', test_date],
        ['Target file name', output_filename],
        ])
    report.add_group_info(
        test_name='Test Summary report',
        title=title,
        test_path='(summary)',
        test_date=test_date
        )

    required_tests = [ t[1]['path'].replace(':', '.') for t in required_tests]  # extract the path names from the test list. This is order sensitive.
    unused_tests = test_cases.keys()
    # print 'Required tests are:', required_tests
    # print 'Available tests are:', unused_tests
    report.enter_group_node('required_tests')
    report.add_group_info(title='Required tests')
    summary = []
    for path in required_tests:
        if path in unused_tests:
            case_info = test_cases[path][0]  # get the most recent test
            summary.append(get_case_summary(case_info))
            unused_tests.remove(path)
            report.add_node(case_info[2])
        else:
            summary.append(('?', path, 'False', 'Test not run'))
    report.exit_node()

    report.add_header('Required test summary')
    report.add_literal_text_block('\n'.join(format_summary(summary)))

    report.enter_group_node('optional_tests')
    report.add_group_info(title='Optional tests')
    summary = []
    for path in unused_tests:
        case_info = test_cases[path][0]
        summary.append(get_case_summary(test_cases[path][0]))
        report.add_node(case_info[2])
    report.exit_node()

    report.add_header('Optional test summary')
    report.add_literal_text_block('\n'.join(format_summary(summary)))

    if output_filename:
        report.write(output_filename, output_formats)

    return report

def generate_combined_summary(input_folder='.', required_tests=[], output_filename=None):
    """
    input folder is a pattern that selects all the folders to be included in the summary (i.e. "./MGADC08_SN????/").
    """
    test_list = [t[1]['path'] for t in required_tests]

    dirs = glob.glob(input_folder+'\\')

    results = []
    print 'Loading test reports...'
    for i,d in enumerate(dirs):
        serial = os.path.split(os.path.split(d)[0])[1]
        print '%i/%i: %s' % (i+1, len(dirs), serial)
        test_cases = load_test_cases(d)
        status = get_test_status(test_cases, test_list)
        passed = ('FAILED', 'Passed')[all(t.passed for t in status)]
        first_fail = ['%s Failed: %s' % (t.name, t.message) for t in status if t.date and not t.passed]
        first_fail = '"'+first_fail[0]+'"' if first_fail else 'N/A'
        first_date = min(t.date for t in status)
        last_date = max(t.date for t in status)
        details =['"%s=%s (%s)"' % (t.name, ('FAILED', 'PASSED')[bool(t.passed)], t.message) for t in status]
        results.append(NameSpace(serial=serial, passed=passed, date=last_date, first_date=first_date, first_fail=first_fail, details=details))
    results.sort(key=lambda t: t.serial)
    print

    strings = []
    for r in results:
        s ='%10s, %s, %s, %s' % (r.serial, r.passed, r.date, r.first_fail)
        print s
        # strings.append(s + ',' + ','.join(r.details))
        strings.append(s)

    if output_filename:
        with open(output_filename, 'w') as f:
            f.write('\n'.join(strings))
        print
        print 'Summary report is saved in %s' % os.path.realpath(output_filename)
        print
    return strings
