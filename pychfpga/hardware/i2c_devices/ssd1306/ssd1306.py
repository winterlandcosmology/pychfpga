# Standard packages
import time
import os

# PyPi packages
import freetype
import numpy as np

# register definitions
SET_CONTRAST = 0x81
SET_ENTIRE_DISP_OFF = 0xA4
SET_ENTIRE_DISP_ON = 0xA5
SET_NORM_INV_OFF = 0xA6
SET_NORM_INV_ON = 0xA7
SET_DISP_OFF = 0xAE
SET_DISP_ON = 0xAF
SET_MEM_ADDR_MODE = 0x20
SET_COL_ADDR = 0x21
SET_PAGE_ADDR = 0x22
SET_DISP_START_LINE = 0x40
SET_SEG_REMAP_OFF = 0xA0 # scans from col 0 to 127
SET_SEG_REMAP_ON = 0xA1 # scans from col 127 to 0
SET_MUX_RATIO = 0xA8
SET_COM_OUT_DIR_UP = 0xC0
SET_COM_OUT_DIR_DOWN = 0xC8
SET_DISP_OFFSET = 0xD3
SET_COM_PIN_CFG = 0xDA
SET_DISP_CLK_DIV = 0xD5
SET_PRECHARGE = 0xD9
SET_VCOM_DESEL = 0xDB
SET_CHARGE_PUMP = 0x8D


class SSD1306:
    """ Class to operate SSD1306-based OLED displays via its I2C interface.

    The class is coded for simplicity and clearly not for ultimate performance. This should be ok, as
    this code is generally run on a fast server and printing speed is generally limited by I2C
    communication with the display.

    We allow ourselves to use numpy as a frame buffer since this is not meant to be run on a
    microcontroller. We represent the array as a one byte per pixel. This simplifies pixel
    addressing and allows simple slices to be used to draw horizontal or vertical lines. We used a
    reshaped view of the frame buffer to access it as page, bit and column.  When it is time to send
    the frame buffer to the display, we slice the pages and columns that has been flagged as dirty,
    and we use a dot product to combines the bits into a byte values using a binary weight vector.
    This leverages the numpy internal optmizations. We then send the resulting dirty bytes to the display,
    which was also configured to move its pointer between the designated rectangular dirty area.

    Printing text is typically 10-20 faster than the time it takes to update the affected region of
    the display, so we don't need to optimize this code more than this.

    Some stats:

    - Font Size=8, 4 characters, rotated:  Print: 0.8 ms, Update: 4.6 ms, Ratio: 5.4x
    - Font Size=2, 18 characters, non-rotated:  Print: 0.5 ms, Update: 6.4 ms, Ratio: 12.4x
    - Font Size=8, 18 characters, non-rotated:  Print: 1.0 ms, Update: 18.0 ms, Ratio: 18.2x
    - Font Size=16, 18 characters, non-rotated:  Print: 3.4 ms, Update: 53.1 ms, Ratio: 15.7x
    - The ratio gets worse for larger text as most of the text falls outside the display area:
    - Font Size=29, 18 characters, non-rotated:  Print: 18.4 ms, Update: 53.1 ms, Ratio: 2.9x

    Parameters:

        i2c (I2C object): i2c object through which communication with the display is performed

        address (int): I2C address of the display

        port (int or tuple): Indicates through which I2C port the display is accesssed. It is an
            integer if we directly access the I2C peripheral identified by `port`, and is a
            (switch_object, switch_port) if we are accessing the displat through one or more I2C
            switches.
    """

    WIDTH = 128
    HEIGHT = 32
    PAGES = HEIGHT // 8

    WHITE = 1
    BLACK = 0


    def __init__(self, i2c, address, port):

        self.i2c = i2c
        self.address = address
        self.port = port

        self.PAGES = self.HEIGHT // 8
        self.bin_weights = np.array([1, 2, 4, 8, 16, 32, 64, 128], dtype=np.uint8) # weight of each bits in a byte
        self.fb = np.zeros((self.HEIGHT, self.WIDTH), np.uint8) # frame buffer, 1 byte per pixel
        self.fb_buf = self.fb.reshape((self.PAGES, 8, self.WIDTH)) # view  of the frame buffer divided by page, bit and column. We'll multiply-and-sum the 'bit' dimension with the binary weights to reduce this axis to bytes'

        self.fb_x0 = 0  # current lowest modified frame buffer column
        self.fb_x1 = self.WIDTH-1  # current highest modified frame buffer column
        self.fb_p0 = 0  # current lowest modified frame buffer page
        self.fb_p1 = self.PAGES-1  # current highest modified frame buffer page

        self.brightness = 0 # dim level: 0= display off, 1: min brightness, 16: max brightness
        self.last_time = time.time()

        # current font information
        self.font_path = os.path.dirname(__file__)  # fonts are in the same folder as this module
        self.font_name = 'Roboto-Medium.ttf'
        # self.font_path = '/home/jfcliche/Downloads/oldschool_pc_font_pack_v2.2_linux/ttf - Mx (mixed outline+bitmap)/'
        # self.font_name = 'Mx437_Acer_VGA_8x8.ttf'
        self.font_size = 8

        self.text_x = 0
        self.text_y = 0
        self.fg = self.WHITE
        self.bg = self.BLACK


    def select(self):
        """
        Selects the proper I2C port to talk to this device.
        """
        self.i2c.select_bus(self.port)

    def write_command(self, data):
        """ Send command byte(s) to the display

        Parameters:

            data (int or list/bytes): integer or list of integers (or bytes) representing the
                command to send to the display. Multiple commands can be sent in the same transaction.
        Note:
            - on the SSD1306 with I2C, a transaction consists of a control byte followed by either command or graphics bytes.
              The control byte contains the continuation bit (bit 7) and command/data bit (bit 6, '0'=command, '1' = data).
              For commands, if the continuation bit is '0', multiple
              commands can be daisy-chained without additional control bytes. If the continuation
              bit is '1', a new control byte is expected after each command (and its arguments), allowing either a new
              command of graphics data to be sent in one transaction
        """
        if isinstance(data, int):
            self.i2c.write_read(self.address, [0x00, data])
        else:
            self.i2c.write_read(self.address, [0x00] + list(data))
            # self.i2c.write_read(self.address, data)

    def write_data(self, data):
            """ Sends graphics data to the display"""
            self.i2c.write_read(self.address, [0x40] + list(data))

    def init(self):
        """ Initializes the display controller to the desired display mode
        """
        # self.write_command(SET_DISP_ON)
        # self.write_command(SET_DISP_OFF)
        # self.write_command((SET_DISP_CLK_DIV, 0x81))

        cmds = (

            SET_DISP_OFF,  # display off
            SET_DISP_CLK_DIV, 0x81, # fosc_freq = 0b1000, divide by 1 (0b0000) (default)
            SET_MUX_RATIO, self.HEIGHT - 1, # Multiplex ratio 1/32 */
            SET_DISP_OFFSET, 0, # Display offset 0
            SET_DISP_START_LINE | 0x00, # Display RAM display start line to 0 (default)

            SET_SEG_REMAP_ON,  # Scan from column addr 127 mapped to 0
            SET_COM_OUT_DIR_DOWN,  # scan from COM[N] to COM0
            SET_COM_PIN_CFG, 0x02, # Standard sequential COM pin assignment, No left-right COM pin swap
            SET_CONTRAST, 0xFF,  # maximum
            SET_ENTIRE_DISP_OFF,  # do not fill entire display, output follows RAM contents
            SET_NORM_INV_OFF,  # display not inverted
            SET_PRECHARGE, 0xf1, # (was 0x22 if self.external_vcc else 0xF1),
            SET_VCOM_DESEL, 0x00,  # Vcomh, 0=.65Vcc (brightest), 0x20=.77vss, 0x30=0x83vcc
            SET_MEM_ADDR_MODE, 0x00, # horizontal addressing mode
            SET_CHARGE_PUMP, 0x10, # Disable charge pump 0x10=disable, 0x14=enable (was 0x10),
            SET_DISP_ON # required after charge pump setting
            )
        self.write_command(cmds)
        # for cmd in cmds:
        #     self.write_command(cmd)

    def set_dirty(self, x0=0, x1=WIDTH-1, p0=0, p1=PAGES-1):
        """Keep track of the lowest and highest modified line and page to optimize the slow display update.

        Parameters:

            x0,x1,p0,p1 (int): coordinates of the rectangle that contains a region that has been
                modified, where x0 and x1 are column numbers and p0 and p1 are page numbers. It is
                required that x1 >= x0 and p1 >= p0. If these parameters are not provided, the whole
                frame buffer is invalidated.
        """
        self.fb_x0 = min(self.fb_x0, x0)
        self.fb_x1 = max(self.fb_x1, x1)
        self.fb_p0 = min(self.fb_p0, p0)
        self.fb_p1 = max(self.fb_p1, p1)

    def update(self, force=False):
        """ Sends the frame buffer to the display.

        If no lines are specified, only the block of lines that were modified since the last call are updated.

        Parameters:

            force (bool): Force the update of the full display

        """
        # t0 =time.time()
        if force:
            self.set_dirty()
        if self.fb_p1 < 0: # skip if display is up to date
            return
        self.write_command((SET_PAGE_ADDR, self.fb_p0, self.fb_p1, SET_COL_ADDR, self.fb_x0, self.fb_x1))
        # extract the submatrix that has been modified, compute byte values for group of 8 lines, flatten and send to display
        b = np.tensordot(self.fb_buf[self.fb_p0: self.fb_p1+1, :, self.fb_x0: self.fb_x1+1], self.bin_weights, axes=(1,0)).flatten()
        # print(f'Updating col {self.fb_x0}-{self.fb_x1}, page {self.fb_p0}-{self.fb_p1} ({(self.fb_x1 - self.fb_x0 + 1 )*(self.fb_p1-self.fb_p0+1)} bytes, real={len(b)} bytes)')
        self.write_data(b)
        # reset dirty area
        self.fb_p1 = self.fb_x1 = -1 # Indicate we are up to date
        self.fb_x0 = self.WIDTH
        self.p0 = self.PAGES
        # print(f'update took {(time.time()-t0)*1e6:.0f} us')

    def display_on(self):
        self.write_command(SET_DISP_ON)

    def display_off(self):
        self.write_command(SET_DISP_OFF)

    def set_contrast(self, contrast):
        self.write_command((SET_CONTRAST, contrast))

    def set_invert(self, invert):
        self.write_command(SET_NORM_INV_OFF if invert else SET_NORM_INV_OFF)

    # def set_scroll(self, nb_offset_cols, start_row, nb_rows, nb_offset_rows, time_interval=100):
    #     """ Not implemented yet"""
    #     # Valid time intervals: 6, 10, 100 or 200 frames
    #     TIME_INTERVALS = {6: 0x00, 10: 0x01, 100: 0x2, 200: 0x3}
    #     if time_interval in TIME_INTERVALS:
    #         self.write_command([0x27, nb_offset_cols, start_row,
    #                            nb_rows, nb_offset_rows, TIME_INTERVALS[time_interval]])

    # def stop_scroll(self):
    #     self.write_command((0x2E,))

    # def start_scroll(self):
    #     self.write_command((0x2F,))



    def set_pixel(self, x, y, color):
        """ Set pixel in the frame buffer to specified color

        Parameters:

            x, y (int): column and row of pixel. Origin is upper left of display. Writes outside the display area are ignored.

            color (int): color of pixel: 0 (off) or 1 (on)

        """
        if x < 0 or x >= self.WIDTH or y < 0 or y >= self.HEIGHT-1:
            return

        p = y >> 3
        self.fb[y, x] = color
        self.set_dirty(x,x,y >> 3,y >> 3)


    def clear(self, update=False):
        """ Clear the frame buffer, and optionally update the display
        """
        self.fb[:] = 0
        self.set_dirty() # all the frame buffer needs to be updated
        self.text_x = self.text_y = 0

        if update:
            self.update()

    def hline(self, x0, x1, y, color = WHITE):
        """ Draws an horizontal line in the frame buffer

        Parameters:

            x0, x1, y (int): coordinates of the line. Line will be drawn between (x0,y) and (x1,y).

        We optimized a little bit by taking advantage of numpy array indexing.
        This is possible because we apply the same bit mask to all cells.

        """
        self.fb[y:y+1, x0:x1+1] = color
        self.set_dirty(x0, x1, y >> 3, y >> 3)

    def vline(self, x, y0, y1, color = WHITE):
        """ Draws an vertical line in the frame buffer

        Parameters:

            x, y0, y1 (int): coordinates of the line. Line will be drawn between (x,y0) and (x,y1).

        We don't bother optimizing using numpy array as we would need to handle the varous bitmasks at the beginning and end of the line.
        We therefore go pixel by pixel. The display is not high, so the loop is generally short.
        """
        self.fb[y0:y1+1, x:x+1] = color
        self.set_dirty(x, x, y0>>3, y1>>3)

    def fill(self, x0, y0, x1, y1, color=WHITE):
        """ Fills the specified area with the specified color

        Parameters:

            x0, y0, x1, y1 (int): coordinates of the corner of the box to be filled.
        """
        self.fb[y0:y1+1, x0:x1+1] = color
        self.set_dirty(x0, x1, y0>>3, y1>>3)

    def draw_glyph(self, x, y, glyph, width, height, fg=WHITE,  bg=BLACK, rotate=False) -> None:
        """ Writes a freetype glyph in the frame buffer.

        Parameters:

            x, y (int): coordinate of the upper-left corner of the glyph

            glyph: freetype glyph object to be written to the frame buffer

            width, height (int): width and height of the region to be written. `width` generally
                includes the space between character to ensure that space is cleared. `height` is
                used to determine the glyph baseline position.

            fg, bg (int): foreground and background colors

            rotate (bool): Draws the glyph with a 90 degree rotation when True

        We draw the glyph pixel by pixel to make the code smaller and the rotation easier, but that could certainly be optimized.

        """
        # t0 = time.ticks_ms()
        fb = self.fb
        bm = glyph.bitmap
        for row in range(height):
            r = row - height + 1 + glyph.bitmap_top # row number within the glyph bitmap taking into account the bitmap offset
            for col in range(width):
                color = (bm.buffer[bm.pitch*r+(col>>3)] >> (7-(col & 7))) & 1 if r >= 0 and r < bm.rows and col < bm.width else 0
                if rotate:
                    self.set_pixel(x+row, y-col, fg if color else bg)
                else:
                    self.set_pixel(x+col, y+row, fg if color else bg)

    def print(self, text, x=None, y=None, fg=None, bg=None, update=False, font_name=None, font_size=None, rotate=False, verbose=0):
        """ Print text in the frame buffer

        Parameters:

            text (str): string to print

            x, y (int): coordinate of where to start to print. If not specified, continues from last current location.

            fg, bg (int): sets the foreground and background color. If not specified, the current colors are used.

            update (bool): if True, the the frame buffer is sent to the display after the print is complete.

            font_name (str): name of the font file to use

            font_size (int): Size of the font to use in pixels. If not specified, the current font is used.

            rotate (bool): if True, the text will be written at a 90 degrees angle (in decreasing y coordinates).
        """
        if font_name:
            self.font_name = font_name
        if font_size:
            self.font_size = font_size
        if x is not None:
            self.text_x = x
        if y is not None:
            self.text_y = y
        if fg is not None:
            self.fg=fg
        if bg is not None:
            self.bg=bg

        t0 = time.time()
        font_height = self.font_size
        face = freetype.Face(os.path.join(self.font_path, self.font_name))
        face.set_pixel_sizes(0, font_height)

        def newline(): # move to next line
            if rotate:
                self.text_y = self.HEIGHT-1
                self.text_x += font_height
            else:
                self.text_x = 0
                self.text_y += font_height

        for c in text:
            if c == '\n': # newline
                newline()
                continue

            # load character bitmap and extract its geometry
            face.load_char(ord(c), freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO)
            glyph = face.glyph
            bitmap = glyph.bitmap
            font_width = bitmap.width
            advance = (face.glyph.linearHoriAdvance+65535)//65536 # horizontal distance to the next character in pixels

            # move to a new line if there is not enough space on the current line for the character
            if (rotate and self.text_y < font_width-1) or (not rotate and self.text_x + font_width -1 >= self.WIDTH):
                newline()

            # print(f' char {c} @ {self.text_x}, {self.text_y}, width={font_width}, adv={advance}')

            self.draw_glyph(self.text_x, self.text_y, glyph, width= advance, height=font_height, fg=self.fg, bg=self.bg, rotate=rotate)

            # increment cursor position
            if rotate:
                self.text_y -= advance
            else:
                self.text_x += advance

        dt1 = time.time()-t0
        # print(f'Print took {dt1*1e3:.1f} ms')

        if update:
            t0 = time.time()
            self.update()
            dt2 = time.time()-t0
            if verbose:
                print(f'Font Size={font_height}, {len(text)} characters:  Print: {dt1*1e3:.1f} ms, Update: {dt2*1e3:.1f} ms, Ratio: {dt2/dt1:.1f}x')
