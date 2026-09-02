"""ANSI 256 colour numbers to RGB.

`terrain.colour` and `civ.colour` hold ANSI 256 integers, because the terminal
client came first. That looks like it should force an `rgb` column onto the
schema for the windowed client's benefit -- but the palette is a formula, so
this conversion is exact and lossless, and the database goes on holding one
representation of one fact. 04_views.sql opens by arguing for exactly that.

The consequence worth having: both front-ends run `render.HEAT` through here,
so an overlay is the same colour in a terminal and in a window.
"""

# The 6x6x6 cube's axis. Not evenly spaced -- the first step is much larger --
# so that the dark end of the cube stays distinguishable.
LEVELS = (0, 95, 135, 175, 215, 255)


def rgb(colour):
    """The RGB triple a terminal would draw for ANSI colour number `colour`."""
    if colour >= 232:                      # 232-255: a 24-step grey ramp
        grey = 8 + 10 * (colour - 232)
        return (grey, grey, grey)
    if colour >= 16:                       # 16-231: the 6x6x6 cube
        n = colour - 16
        return (LEVELS[n // 36], LEVELS[n // 6 % 6], LEVELS[n % 6])
    # 0-15 are whatever the terminal's theme sets them to, so any answer here
    # is a guess. Nothing in sql/ uses them; this branch only makes the
    # function total, and follows the convention that bit 0 is red.
    full = 255 if colour >= 8 else 128
    return tuple(full if colour >> bit & 1 else 0 for bit in range(3))


def dim(colour, amount):
    """Darken towards black. 0.0 leaves it alone, 1.0 is black."""
    return tuple(round(channel * (1.0 - amount)) for channel in colour)


def readable_on(background):
    """Black or white, whichever will be legible on `background`.

    Rec. 601 luminance. Unit glyphs sit on civ colours the database chose and
    city populations on terrain, so neither can be given a fixed ink colour and
    still be readable from the back of a lecture hall.
    """
    red, green, blue = background
    bright = 0.299 * red + 0.587 * green + 0.114 * blue
    return (0, 0, 0) if bright > 140 else (255, 255, 255)
