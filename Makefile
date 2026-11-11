# contract/ against contract.lock, and moving to another published contract (see tools/contract/fetch.py).
.PHONY: contract contract-update contract-test

contract:
	python3 tools/contract/fetch.py

contract-update:
	python3 tools/contract/fetch.py --update $(VERSION)

# Everything that reads contract/, against whatever it holds: an unpublished contract
# (CONTRACT_DIR=<dir> make contract-test, as os pull requests run it) or the one just pinned
# (after make contract-update). The bridge tests, the TypeScript client generated from its
# OpenAPI, and conformance of the Python and TypeScript scaffolds. Needs Go, Node and
# tests/requirements.txt.
BIN := $(CURDIR)/build/contract-test
contract-test: contract
	python3 -m pytest $(if $(CONTRACT_DIR),--deselect tests/test_contract.py::test_contract_is_the_one_contract_lock_pins)
	cd clients/typescript && npm ci --no-audit --no-fund && npm run generate && npm run typecheck && npm test && npm run build
	rm -rf $(BIN) && cd cli && go build -o $(BIN)/constellation ./cmd/constellation
	python3 -m pip install --quiet ./clients/python
	$(BIN)/constellation bridge new py-bridge --lang python --dir $(BIN)/py-bridge
	cd $(BIN)/py-bridge && $(BIN)/constellation conformance -- python3 bridge.py
	$(BIN)/constellation bridge new ts-bridge --lang ts --dir $(BIN)/ts-bridge
	cd $(BIN)/ts-bridge && npm install --no-audit --no-fund $(CURDIR)/clients/typescript \
	  && $(BIN)/constellation conformance -- node --experimental-strip-types bridge.ts
