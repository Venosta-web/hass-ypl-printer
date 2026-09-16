# Y50P capture fixtures

Unmodified copies of the hardware captures in yplib's `captures/` directory at
commit `4748c21393b30f78defed256a7394d0960ad9a07`. yplib is MIT licensed,
copyright 2026 Shaun Lastra; the full notice is in
[`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md).

Each file is the host-to-printer byte stream of one session recorded from the
vendor app. `tests/test_protocol.py` checks each size and SHA-256 before use.

| File | Bytes | SHA-256 | Source | Use |
| --- | ---: | --- | --- | --- |
| `y50p-flashlabel-label.bin` | 3,106 | `c2838f27da086f993da6be3793d9f762e60936746f14249e9fc36792a2ce8465` | [upstream](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/captures/y50p-flashlabel-label.bin) | Rebuilt byte for byte; compression rate `0x13`, full trailer |
| `y50p-horizontal-line.bin` | 1,507 | `0d4e7b64a07b2767eaa96ab05206241e560b009e2da9e337bde6e176a08dcda0` | [upstream](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/captures/y50p-horizontal-line.bin) | Rebuilt byte for byte; compression rate `0x08`; the recording stops after the first trailer frame (`05/21`) |
| `y50p-vertical-line.bin` | 1,581 | `dd19f0e9fbedf097ad27e37a8831b286e47081c9634476e0d1a2c6e4e69d7c0d` | [upstream](https://github.com/slastra/yplib/blob/4748c21393b30f78defed256a7394d0960ad9a07/captures/y50p-vertical-line.bin) | Decoded only; the recording starts mid-session |
