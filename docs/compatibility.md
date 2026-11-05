# Hardware compatibility

Compatibility is negotiated from protocol capabilities, not guessed from a manufacturer name. UA/firmware strings are untrusted hints. Antminer, Whatsminer, Avalon, Vnish and Braiins are **pilot targets**, not a certified-device list.

## Implemented behavior

- BIP310 mask syntax and requested-bit bounds are checked. Alternate upstreams negotiate the **same** mask as the miner; a wider mask is not accepted as equivalent. A smaller bit count is reported as degraded operation rather than hidden.
- Independent configure extensions retain earlier version-rolling state. Accumulated configuration is bounded to 32 extension codes, 128 parameters and the configured miner-message byte budget, so repeated small requests cannot grow session state without bound.
- `subscribe-extranonce` in configure and `mining.extranonce.subscribe` advertise hot-change support. An active dynamic version-mask change takes the conservative reconnect path; an inactive incompatible fee channel cannot tear down the user socket.
- Default client `extranonce2` maximum is four bytes, configurable with `--asic-extranonce2-min/--asic-extranonce2-max`. Extra upstream bytes become a fixed prefix appended to downstream `extranonce1`; submits restore the prefix to upstream `extranonce2`. Coinbase bytes and the Bitcoin header remain identical. An upstream below the configured minimum is incompatible, not silently padded with a different PoW.
- An optional fee route below that minimum is rejected during preparation while user mining continues. Mandatory-fee policy still applies if the incompatible route remains unavailable.
- A peer disconnect within three seconds of a hot extranonce/difficulty change, before an accepted share, records a conservative reconnect profile. Proxy shutdowns, watchdog closures and intentional reconnects do not count. This timing correlation does **not** prove firmware causality; network faults can cause false positives.
- Profiles use hashed `(IP, worker name, agent)` identities, are bounded to 2048 entries, expire in 24 hours and persist atomically in `state/compatibility.json`. Multiple devices using identical names behind one IP may share a profile. Use unique worker names. No pool passwords or raw worker/IP values are persisted in this file.
- `--hot-switch-mode reconnect` provides an operator override. Non-hot/learned fallback retains the configured long user-window cooldown; hot-capable sessions use the live context switch.

The byte-translation mechanism has a two-pool cryptographic regression with upstream widths 6 and 8 and downstream width 4. It does not establish the byte limits or chip behavior of physical devices. In particular, do not infer universal empty-extranonce support from another proxy's settings.

## Qualification matrix

| Family / firmware | Protocol regressions | Physical qualification |
| --- | --- | --- |
| Antminer stock | BIP310, exact masks, reconnect and nonce-width tests | Pending exact model/firmware/pool |
| Whatsminer stock | Generic Stratum V1 and declared capabilities | Pending exact model/firmware/pool |
| Avalon stock | Generic Stratum V1; no guessed vendor workaround | Pending extranonce and reconnect tests |
| Vnish | Version-rolling/capability paths, synthetic only | Pending specific release and hardware |
| Braiins OS | BIP310 capability path, synthetic only | Pending specific release and hardware |

Record: model, firmware build, agent string if available, pool/port/TLS, negotiated masks and nonce sizes, hours observed, rejects and forced reconnects. Redact wallets, passwords, tokens and public IPs before sharing traces.

Sources: [BIP310](https://github.com/bitcoin/bips/blob/master/bip-0310.mediawiki), [Braiins extranonce configuration caveats](https://academy.braiins.com/farm-proxy/configuration). We use these as protocol/design references, not as certification of our code.
