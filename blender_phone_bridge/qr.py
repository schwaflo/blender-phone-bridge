# SPDX-License-Identifier: MIT
"""Minimal QR code encoder (byte mode) without external dependencies.

Based on the algorithm described in ISO/IEC 18004 and the well known
reference implementation by Project Nayuki. Only what is needed to encode
a URL is implemented: byte mode, error correction levels L/M/Q/H,
versions 1-40 and automatic mask selection.
"""

_ECC_CODEWORDS_PER_BLOCK = {
    "L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    "Q": (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    "H": (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
}
_NUM_ERROR_CORRECTION_BLOCKS = {
    "L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    "Q": (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    "H": (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
}
_FORMAT_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}


def _num_raw_data_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver, ecl):
    return (_num_raw_data_modules(ver) // 8
            - _ECC_CODEWORDS_PER_BLOCK[ecl][ver] * _NUM_ERROR_CORRECTION_BLOCKS[ecl][ver])


def _rs_multiply(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_multiply(coef, factor)
    return result


def _get_bit(x, i):
    return ((x >> i) & 1) != 0


class QrCode:
    def __init__(self, version, ecl, datacodewords, mask=-1):
        self.version = version
        self.size = version * 4 + 17
        self.ecl = ecl
        self.modules = [[False] * self.size for _ in range(self.size)]
        self._isfunction = [[False] * self.size for _ in range(self.size)]

        self._draw_function_patterns()
        allcodewords = self._add_ecc_and_interleave(list(datacodewords))
        self._draw_codewords(allcodewords)

        if mask == -1:
            minpenalty = 1 << 32
            for i in range(8):
                self._apply_mask(i)
                self._draw_format_bits(i)
                penalty = self._get_penalty_score()
                if penalty < minpenalty:
                    mask = i
                    minpenalty = penalty
                self._apply_mask(i)  # undo
        self.mask = mask
        self._apply_mask(mask)
        self._draw_format_bits(mask)
        del self._isfunction

    # ---- construction -------------------------------------------------
    def _set_function_module(self, x, y, isdark):
        self.modules[y][x] = isdark
        self._isfunction[y][x] = True

    def _draw_function_patterns(self):
        for i in range(self.size):
            self._set_function_module(6, i, i % 2 == 0)
            self._set_function_module(i, 6, i % 2 == 0)
        self._draw_finder_pattern(3, 3)
        self._draw_finder_pattern(self.size - 4, 3)
        self._draw_finder_pattern(3, self.size - 4)
        alignpatpos = self._get_alignment_pattern_positions()
        numalign = len(alignpatpos)
        skips = ((0, 0), (0, numalign - 1), (numalign - 1, 0))
        for i in range(numalign):
            for j in range(numalign):
                if (i, j) not in skips:
                    self._draw_alignment_pattern(alignpatpos[i], alignpatpos[j])
        self._draw_format_bits(0)
        self._draw_version()

    def _draw_format_bits(self, mask):
        data = _FORMAT_BITS[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        for i in range(0, 6):
            self._set_function_module(8, i, _get_bit(bits, i))
        self._set_function_module(8, 7, _get_bit(bits, 6))
        self._set_function_module(8, 8, _get_bit(bits, 7))
        self._set_function_module(7, 8, _get_bit(bits, 8))
        for i in range(9, 15):
            self._set_function_module(14 - i, 8, _get_bit(bits, i))
        for i in range(0, 8):
            self._set_function_module(self.size - 1 - i, 8, _get_bit(bits, i))
        for i in range(8, 15):
            self._set_function_module(8, self.size - 15 + i, _get_bit(bits, i))
        self._set_function_module(8, self.size - 8, True)

    def _draw_version(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            bit = _get_bit(bits, i)
            a = self.size - 11 + i % 3
            b = i // 3
            self._set_function_module(a, b, bit)
            self._set_function_module(b, a, bit)

    def _draw_finder_pattern(self, x, y):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = x + dx, y + dy
                if 0 <= xx < self.size and 0 <= yy < self.size:
                    self._set_function_module(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))

    def _draw_alignment_pattern(self, x, y):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self._set_function_module(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)

    def _get_alignment_pattern_positions(self):
        ver = self.version
        if ver == 1:
            return []
        numalign = ver // 7 + 2
        step = (ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
        result = [(self.size - 7 - i * step) for i in range(numalign - 1)] + [6]
        return list(reversed(result))

    def _add_ecc_and_interleave(self, data):
        ver, ecl = self.version, self.ecl
        numblocks = _NUM_ERROR_CORRECTION_BLOCKS[ecl][ver]
        blockecclen = _ECC_CODEWORDS_PER_BLOCK[ecl][ver]
        rawcodewords = _num_raw_data_modules(ver) // 8
        numshortblocks = numblocks - rawcodewords % numblocks
        shortblocklen = rawcodewords // numblocks

        blocks = []
        rsdiv = _rs_divisor(blockecclen)
        k = 0
        for i in range(numblocks):
            dat = data[k: k + shortblocklen - blockecclen + (0 if i < numshortblocks else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, rsdiv)
            if i < numshortblocks:
                dat.append(0)
            blocks.append(dat + ecc)

        result = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if (i != shortblocklen - blockecclen) or (j >= numshortblocks):
                    result.append(blk[i])
        return result

    def _draw_codewords(self, data):
        i = 0
        for right in range(self.size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = (self.size - 1 - vert) if upward else vert
                    if not self._isfunction[y][x] and i < len(data) * 8:
                        self.modules[y][x] = _get_bit(data[i >> 3], 7 - (i & 7))
                        i += 1

    _MASK_PATTERNS = (
        lambda x, y: (x + y) % 2,
        lambda x, y: y % 2,
        lambda x, y: x % 3,
        lambda x, y: (x + y) % 3,
        lambda x, y: (x // 3 + y // 2) % 2,
        lambda x, y: x * y % 2 + x * y % 3,
        lambda x, y: (x * y % 2 + x * y % 3) % 2,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
    )

    def _apply_mask(self, mask):
        masker = QrCode._MASK_PATTERNS[mask]
        for y in range(self.size):
            for x in range(self.size):
                self.modules[y][x] ^= (masker(x, y) == 0) and (not self._isfunction[y][x])

    def _get_penalty_score(self):
        result = 0
        size = self.size
        modules = self.modules

        def finder_penalty_count(runhistory):
            n = runhistory[1]
            core = n > 0 and runhistory[2] == runhistory[4] == runhistory[5] == n and runhistory[3] == n * 3
            return ((1 if core and runhistory[0] >= n * 4 and runhistory[6] >= n else 0)
                    + (1 if core and runhistory[6] >= n * 4 and runhistory[0] >= n else 0))

        def finder_penalty_add_history(currentrunlength, runhistory):
            if runhistory[0] == 0:
                currentrunlength += size
            runhistory.pop()
            runhistory.insert(0, currentrunlength)

        def finder_penalty_terminate_and_count(currentruncolor, currentrunlength, runhistory):
            if currentruncolor:
                finder_penalty_add_history(currentrunlength, runhistory)
                currentrunlength = 0
            currentrunlength += size
            finder_penalty_add_history(currentrunlength, runhistory)
            return finder_penalty_count(runhistory)

        for get in (lambda a, b: modules[a][b], lambda a, b: modules[b][a]):
            for y in range(size):
                runcolor = False
                runx = 0
                runhistory = [0] * 7
                for x in range(size):
                    if get(y, x) == runcolor:
                        runx += 1
                        if runx == 5:
                            result += 3
                        elif runx > 5:
                            result += 1
                    else:
                        finder_penalty_add_history(runx, runhistory)
                        if not runcolor:
                            result += finder_penalty_count(runhistory) * 40
                        runcolor = get(y, x)
                        runx = 1
                result += finder_penalty_terminate_and_count(runcolor, runx, runhistory) * 40

        for y in range(size - 1):
            for x in range(size - 1):
                c = modules[y][x]
                if c == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                    result += 3

        dark = sum((1 if c else 0) for row in modules for c in row)
        total = size ** 2
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        result += k * 10
        return result


def encode(text, ecl="M"):
    """Return a QrCode for the given text (UTF-8, byte mode)."""
    data = text.encode("utf-8")
    for version in range(1, 41):
        capacity_bits = _num_data_codewords(version, ecl) * 8
        cc_bits = 8 if version <= 9 else 16
        used = 4 + cc_bits + len(data) * 8
        if used <= capacity_bits:
            break
    else:
        raise ValueError("Text zu lang für einen QR-Code")

    bits = []

    def append(val, n):
        for i in reversed(range(n)):
            bits.append((val >> i) & 1)

    append(0x4, 4)  # byte mode
    append(len(data), cc_bits)
    for b in data:
        append(b, 8)
    append(0, min(4, capacity_bits - len(bits)))
    append(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity_bits:
        append(pad, 8)
        pad ^= 0xEC ^ 0x11

    codewords = [0] * (len(bits) // 8)
    for i, bit in enumerate(bits):
        codewords[i >> 3] |= bit << (7 - (i & 7))
    return QrCode(version, ecl, codewords)


def matrix(text, ecl="M"):
    """Return the module matrix as list of rows (True = dark)."""
    return encode(text, ecl).modules
