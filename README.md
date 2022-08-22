# aioice-no-netifaces

`aioice-no-netifaces` is a fork from [aioice](https://github.com/aiortc/aioice), which does not require netifaces library.

## Why?

- Netifaces [is no longer maintained](https://github.com/al45tair/netifaces)
- Installing on Python3.10 requires Microsoft Visual C++ 14.0 or greater on Windows, as there are no pre built binaries

This fork gets the host IP address with pure Python [workaround](https://stackoverflow.com/a/28950776), which does not require Microsoft Visual C++.

## Caveats:

- This library does not support ipv6 (which the original `aioice` does support)
- Currently this library uses only the primary ipv4 address (`aioice` can use all of the ip4 addresses)

## License

`aioice-no-netifaces` is released under the same BSD license as the original `aioice` library.

## Development

Install: `pip install .[dev]`

Build: `python setup.py bdist_wheel`

Release: `twine upload dist/*` 