# Golden job fixtures

`specimen-chunks.hex` holds the complete job the `ypl_printer.print` action
builds for the acceptance specimen (spec §8). It has one 20-byte BLE chunk per
line, as lowercase hex, in send order. The last chunk is shorter. These are the
bytes CI expects the hardware test to send (spec §9, §11 step 3).

| Fact | Value |
| --- | --- |
| Job bytes | 4,124 |
| Chunks | 207 |
| Job SHA-256 | `0512f28c15f61a2804eb887a89960638d39f15d96fb1e173e81540e117b08227` |
| File SHA-256 | `0f04ee091b8adb4286ce207fd4b642e031fefa1ef6c0ac1421995cf5dcbb37d8` |

`tests/test_transport.py` pins all four facts. A change to any of them is a
change to the renderer or protocol contract. It is never a fixture refresh.
