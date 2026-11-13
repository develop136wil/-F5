"""Check that each ICO image is embedded verbatim in a Windows EXE."""
import argparse
from pathlib import Path
import struct

import pefile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('exe')
parser.add_argument('ico')
args = parser.parse_args()
ico = Path(args.ico).read_bytes()
reserved, kind, count = struct.unpack_from('<HHH', ico)
assert reserved == 0 and kind == 1 and count > 0
expected = []
for index in range(count):
    size, offset = struct.unpack_from('<II', ico, 6 + index * 16 + 8)
    expected.append(ico[offset:offset + size])
with pefile.PE(args.exe) as exe:
    images = []
    for entry in exe.DIRECTORY_ENTRY_RESOURCE.entries:
        if entry.id != pefile.RESOURCE_TYPE['RT_ICON']:
            continue
        for icon in entry.directory.entries:
            for language in icon.directory.entries:
                data = language.data.struct
                images.append(exe.get_data(data.OffsetToData, data.Size))
    assert all(data in images for data in expected), 'EXE icon differs from supplied ICO'
print(f'PASS: all {count} ICO images embedded in EXE')
