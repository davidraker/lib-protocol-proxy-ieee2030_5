# Source of the vendored IEEE 2030.5 models

`sep.py`, `enums.py` and `constants.py` are copied unchanged from the `ieee_2030_5.models` package of the
GridAPPS-D 2030.5 server distribution:

* Distribution: `gridappsd-2030_5` version `0.0.2a37` (PyPI wheel `gridappsd_2030_5-0.0.2a37-py3-none-any.whl`)
* Home page: https://github.com/GRIDAPPSD/gridappsd-2030_5
* Author: C. Allwardt, Battelle Memorial Institute

`sep.py` is the xsdata (dataclasses output) rendering of the IEEE 2030.5-2018 `sep.xsd` schema: 283 types, element
and attribute names as in the schema, hex-binary values as `bytes` with `format: base16`, times as `int` seconds.
It depends only on the standard library; the xsdata runtime is needed only by `xml.py` to serialize and parse.

## Licence

The wheel's `METADATA` declares `License: BSD-3-Clause` while the `LICENSE` file it ships is the Apache License 2.0
text. Both are permissive and compatible with this package's Apache-2.0 licence; the shipped `LICENSE` text is kept
alongside the models as `LICENSE` and the attribution above satisfies either.

## Regenerating

Do not edit `sep.py` by hand. To regenerate from the schema (for example for the 2023 edition):

```shell
pip install "xsdata[cli]"
xsdata generate sep.xsd --package protocol_proxy.protocol.ieee2030_5.models --structure-style single-package \
    --output dataclasses
```

then review the diff for renamed fields used by `convert.py` (`DERStatus.genConnectStatus`, quantity types with
`value`/`multiplier`, the status structs with `dateTime`/`value`).
