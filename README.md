# slixmpp-omemo - Slixmpp OMEMO plugin #

A plugin for slixmpp offering the [OMEMO Multi-End Message and Object Encryption protocol](https://xmpp.org/extensions/xep-0384.html), based on [python-omemo](https://github.com/Syndace/python-omemo).

## OMEMO protocol version support ##

Currently supports OMEMO in the `eu.siacs.conversations.axolotl` namespace.
Support for OMEMO in the `omemo:2` namespace is prepared and will be enabled as soon as Slixmpp gains support for [XEP-0420: Stanza Content Encryption](https://xmpp.org/extensions/xep-0420.html).

## Trust ##

Supports [Blind Trust Before Verification](https://gultsch.de/trust.html) and manual trust management.

## Installation ##

Install the latest release using pip (`pip install slixmpp_omemo`) or manually from source by running `pip install .` in the cloned repository.

## Testing, Type Checks and Linting ##

slixmpp-omemo uses [pytest](https://docs.pytest.org/en/latest/) as its testing framework, [mypy](http://mypy-lang.org/) for static type checks and both [pylint](https://pylint.pycqa.org/en/latest/) and [Flake8](https://flake8.pycqa.org/en/latest/) for linting. All tests/checks can be run locally with the following commands:

```sh
$ pip install --upgrade pytest pytest-asyncio pytest-cov mypy pylint flake8
$ mypy --strict --disable-error-code str-bytes-safe --untyped-calls-exclude=slixmpp slixmpp_omemo/ setup.py examples/ tests/
$ pylint slixmpp_omemo/ setup.py examples/ tests/
$ flake8 slixmpp_omemo/ setup.py examples/ tests/
$ pytest --cov=slixmpp_omemo --cov-report term-missing:skip-covered
```

## Getting Started ##

Refer to the documentation on [readthedocs.io](https://slixmpp-omemo.readthedocs.io/), or build/view it locally in the `docs/` directory. To build the docs locally, install the requirements listed in `docs/requirements.txt`, e.g. using `pip install -r docs/requirements.txt`, and then run `make html` from within the `docs/` directory. The documentation can then be found in `docs/_build/html/`.