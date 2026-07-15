# Bilibili Protocol Sources

BLREC uses independently implemented, fixture-tested protocol adapters. These
interfaces are unofficial and may change without notice; automated tests never
perform real Bilibili writes.

## Pinned references

- [`biliup/biliup@18c5bf0`](https://github.com/biliup/biliup/tree/18c5bf086e943e07e9d88a905d2e5d407d6305bb), MIT License. The credential and UPOS implementations were used to verify BiliTV QR parameters, token status and refresh endpoints, APP submission signing, and multipart upload request shapes.
- [`mwxmmy/biliupforjava@a366e4f`](https://github.com/mwxmmy/biliupforjava/tree/a366e4f1f86bfd1c69a9b6cc66c372d2f6da7e1e), Apache-2.0. The Java implementation was used to cross-check the TV QR flow, the returned token/Cookie bundle, scheduled renewal behavior, Web Cookie/CSRF upload, comments, and video danmaku calls.
- [`BACNext/bilibili-API-collect-backup@cfc5fdd`](https://github.com/BACNext/bilibili-API-collect-backup/tree/cfc5fddcc8a94b74d91970bb5b4eaeb349addc47), used only to cross-check endpoint parameters and documented error codes.

No source file was copied verbatim. The Python implementation keeps BiliTV
signing, Web Cookie/CSRF/WBI authentication, and server-issued UPOS credentials
in separate scopes. If a pinned contract stops matching, the affected feature
must pause instead of trying alternate identities or endpoints automatically.
