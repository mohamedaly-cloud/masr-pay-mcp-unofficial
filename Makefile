# `sam build` calls these targets (BuildMethod: makefile in infra/template.yaml).
# Pinned versions come from infra/constraints.txt; Lambda runs x86_64 Python 3.12.
PIP = python3.12 -m pip install --quiet --no-compile --disable-pip-version-check \
	--constraint infra/constraints.txt --target "$(ARTIFACTS_DIR)"

build-McpFunction:
	$(PIP) .
	cp infra/run-mcp.sh "$(ARTIFACTS_DIR)/"

build-MockFunction:
	$(PIP) ".[mock]"
	cp infra/run-mock.sh "$(ARTIFACTS_DIR)/"

build-DemoFunction:
	$(PIP) ".[demo]"
	cp -r demo_site "$(ARTIFACTS_DIR)/"
	cp infra/run-demo.sh "$(ARTIFACTS_DIR)/"
