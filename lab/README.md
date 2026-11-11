# Local lab

Synthetic pools, proofs and load helpers for development. Run commands from
the repository root. `python -m lab.load_test` measures an echo transport;
it is not a physical ASIC or farm-throughput certification.

`same_job_probe.py` is a separate opt-in live experiment for explicitly
authorized operator-owned accounts. It is not started by the proxy, installer
or test suite and does not establish a supported cross-account fee mode.

See [lab guide](../docs/engineering/lab.md) for scenarios and limits.
