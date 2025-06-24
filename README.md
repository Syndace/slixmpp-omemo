[![PyPI](https://img.shields.io/pypi/v/slixmpp_omemo.svg)](https://pypi.org/project/slixmpp_omemo/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/slixmpp_omemo.svg)](https://pypi.org/project/slixmpp_omemo/)
[![Build Status](https://github.com/Syndace/slixmpp-omemo/actions/workflows/test-and-publish.yml/badge.svg)](https://github.com/Syndace/slixmpp-omemo/actions/workflows/test-and-publish.yml)
[![Documentation Status](https://readthedocs.org/projects/slixmpp-omemo/badge/?version=latest)](https://slixmpp-omemo.readthedocs.io/)

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
$ pip install --upgrade .[test,lint]
$ mypy slixmpp_omemo/ examples/ tests/
$ pylint slixmpp_omemo/ examples/ tests/
$ flake8 slixmpp_omemo/ examples/ tests/
$ pytest
```

## Getting Started ##

Refer to the documentation on [readthedocs.io](https://slixmpp-omemo.readthedocs.io/), or build it locally. Additional requirements to build the docs can be installed using `pip install .[docs]`. With all dependencies installed, run `make html` in the `docs/` directory. The documentation can then be found in `docs/_build/html/`.
