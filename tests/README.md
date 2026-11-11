# Tests

From the repository root, run `python -m pytest -q`. This includes the
function-based quantization tests that unittest discovery alone would omit.
CI runs the suite on Windows and Ubuntu 24.04.

The enterprise soak is opt-in. Its synthetic result does not certify a
physical fleet. Fixture certificates and fixed sample credentials are public
test data; production secrets, state and customer traces must not be added.

See [recorded results and qualification criteria](../docs/engineering/testing.md).
