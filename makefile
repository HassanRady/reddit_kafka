APP_DIR := .

test-all: test-venv
	. $(APP_DIR)/.venv/bin/activate \
	&& pytest -v tests/unit \
	&& pytest -v tests/integration \
	&& pytest -v tests/e2e

test-unit: test-venv  
	. $(APP_DIR)/.venv/bin/activate \
	&& pytest -v tests/unit

test-integration: test-venv  
	. $(APP_DIR)/.venv/bin/activate \
	&& pytest -v tests/integration

test-e2e: test-venv
	. $(APP_DIR)/.venv/bin/activate \
	&& pytest -v tests/e2e

dev-venv: $(APP_DIR)/.venv/touchfile
$(APP_DIR)/.venv/touchfile: $(APP_DIR)/requirements.txt
	python3 -m venv $(APP_DIR)/.venv
	. $(APP_DIR)/.venv/bin/activate; pip install -Ur $(APP_DIR)/requirements.txt
	touch $(APP_DIR)/.venv/touchfile

test-venv: $(APP_DIR)/.venv/touchfile
$(APP_DIR)/.venv/touchfile: $(APP_DIR)/test-requirements.txt $(APP_DIR)/requirements.txt
	python3 -m venv $(APP_DIR)/.venv
	. $(APP_DIR)/.venv/bin/activate; pip install -Ur $(APP_DIR)/requirements.txt pip install -Ur $(APP_DIR)/test-requirements.txt
	touch $(APP_DIR)/.venv/touchfile