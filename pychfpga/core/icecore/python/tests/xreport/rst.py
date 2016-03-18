from StringIO import StringIO
import datetime
import docutils
import textwrap
import os.path

class rST():
    """
    Allows the creation of very simple ReStructuredText
    documents.

    Based on reStructuredText.py developed by Kevin MacDermid, August 2014
    """
    def __init__(self, base_filename=None, width=160):
        """
        Creates a new reStructuredText document.
        """
        self.width = width
        if base_filename is None:
            self.base_filename = datetime.datetime.now().strftime('%Y-%m-%d_%Hh%Mm%Ss')
        else:
            self.base_filename = base_filename
        self.rst = StringIO()
        self.attachments = {}  # {tag:data, ...}

    def __str__(self):
        return self.rst.getvalue()

    def __iadd__(self, string):
        self.add(string)
        return self

    def write(self, writer_name=None):
        """ Write the RestructuredText text and attachments (images) to disk.
        If ``writer_name`` is specified, the RestructuredText is converted
        with docutil libraty using the format supported by the specified
        writer.

        """
        if writer_name is not None:
            doc = docutils.core.publish_string(self.rst.getvalue(), writer_name=writer_name)
        else:
            doc = self.rst.getvalue()

        with open(self.base_filename + '.rst', 'w') as f:
            f.write(doc)

        base_path = os.path.split(self.base_filename)[0]
        self.write_attachments(base_path)

    def write_attachments(self, folder):
        base_filename = os.path.split(self.base_filename)[1]

        for (tag, data) in self.attachments.items():
            attachment_filename = os.path.join(folder, base_filename + '_' + tag)
            with open(attachment_filename, 'wb') as f:
                f.write(data)

    def add(self, string):
        self.rst.write(string)

    def add_all(self, string_list):
        for string in string_list:
            self.rst.write(string)

    def add_attachment(self, filename, data):
        self.attachments[filename] = data

    def add_toc(self, depth=2, toc_title='**Local Table of Contents**', local=False):
        self.add('\n')
        self.add('.. class:: center\n')
        self.add('\n')
        self.add('%s\n' % toc_title)
        self.add('\n')
        self.add('.. contents::\n')
        if local:
            self.add('    :local:\n')
        self.add('    :depth: %i\n' % depth)

    def add_section(self, text, level):
        adornment = ('==', '--', '=', '-', '~', '^', '.' )[level]
        self.add('\n\n')
        if len(adornment) > 1:
            self.add('%s\n' % (adornment[0] * len(text)))
        self.add('%s\n' % (text))
        self.add('%s\n' % (adornment[0] * len(text)))

    def add_binary_image(self, image, format='png', tag=None, caption=None):
        if tag is None:
            tag = 'image%i' % len(self.attachments)
        tag = tag + '.' + format
        self.add_attachment(tag, image)
        image_uri = self.base_filename + '_' + tag
        self.add_image(image_uri, caption=caption)

    def add_image(self, image_path, height=None, width=None, align='center', caption=None):
        '''
        Adds an image to the reStruturedText document specified at instantiation

        Args:
            image_path: The absolute path to the image
            height: (optional) The height of the image. String including units. e.g. '100 px'
            width: (optional) The width of the image. String including units. e.g. '100 px'
            align: (optional) The alignment of the image. e.g. 'left', 'right', 'center' (default)
        '''
        self.add('\n')
        local_path = os.path.split(image_path)[1]
        self.add(".. figure:: %s \n" % (local_path))
        if height is not None:
            self.add("   :height: %s\n" % (height))
        if width is not None:
            self.add("   :width: %s\n" % (width))
        self.add("   :align: %s\n" % (align))
        self.add('\n')
        if caption:
            for line in caption.split('\n'):
                self.add('   ' + line + '\n')
            self.add('\n')

    def add_literal_block(self, text):
        self.add('\n')
        if text:  # don't make an empty literal block: this is invalid ResturcturedText
            self.add('::\n')
            self.add('\n')
            for line in text.replace('\r', '\n').split('\n'):
                for splitline in textwrap.wrap(line, self.width):
                    self.add('    ' + splitline + '\n')
        self.add('\n')

    def add_table(self, grid, header=False):
        '''
        Writes a table from elements in grid.
        Shamelessly stolen from:
            http://stackoverflow.com/questions/11347505/what-are-some-approaches-to-outputting-a-python-data-structure-to-restructuredte
        Args:
            grid: A list of tuples containing the table elements.
                Must be square and first line is the table header.
            header: A flag to make the first row a header row
        Output:
            Writes the table to the the given restructured text file.
        '''

        nonzero_grid = []
        single_elements = []
        for row in grid:
            if len(row) > 1:
                nonzero_grid.append(row)
            else:
                single_elements.append(str(row[0]))
        num_cols   = len(nonzero_grid[-1])
        body_cell_width = 2 + max(reduce(lambda x,y: x+y, [[len(str(item)) for item in row] for row in nonzero_grid], []))
        if single_elements:
            biggest_single_width = len(max(single_elements))
            if num_cols * body_cell_width > biggest_single_width:
                cell_width = body_cell_width
            else:
                cell_width = biggest_single_width
        else:
            cell_width = body_cell_width
        rst = self._table_div(num_cols, cell_width, 0)
        total_cell_width = num_cols*cell_width

        #A table with one row is malformed if it has a header
        if len(grid) > 1:
            header_flag = header
        else:
            header_flag = False
        for i, row in enumerate(grid):
            if isinstance(header, int):
                if i==header-1:
                    header_flag = True
                else:
                    header_flag = False
            if len(row) > 1:
                use_cw = (total_cell_width/len(row)) - 1
            else:
                use_cw = (total_cell_width/len(row)) + num_cols-2  ## Since the "|"s also produce an offset
            rst += '| ' + '| '.join([self._normalize_cell(str(x), use_cw) for x in row]) + '|\n'
            rst += self._table_div(num_cols, cell_width, header_flag)
            header_flag = False
        rst += '\n'
        self.add(rst)

    def _table_div(self, num_cols, col_width, header_flag):
        '''
        A table writing helper function, see write_table.
        '''
        if header_flag:
            return num_cols*('+' + (col_width)*'=') + '+\n'
        else:
            return num_cols*('+' + (col_width)*'-') + '+\n'

    def _normalize_cell(self, string, length):
        '''
        A table writing helper function, see write_table.
        '''
        return string + ((length - len(string)) * ' ')
