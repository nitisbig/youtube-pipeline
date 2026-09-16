.PHONY: help demo test lint install clean

help:
	@echo "make demo     - offline run on the mock provider (no API key)"
	@echo "make test     - run the contract tests"
	@echo "make install  - pip install -e . (adds the 'ytscript' command)"
	@echo "make clean    - remove generated output and caches"

demo:
	python3 -m ytscript --topic "Dog Psychology" --generate \
		--provider mock --beats 12 --chunk-size 6 --outdir out/demo -v

test:
	python3 -m unittest discover -s tests -v

install:
	python3 -m pip install -e .

clean:
	rm -rf out build dist *.egg-info .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
