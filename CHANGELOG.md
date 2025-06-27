# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 27th of June, 2025

### Added
- Gracefully shutdown the SessionManager in plugin_end
- Add MUC support to the example echo client
- Support for migration of legacy storage data

### Changed
- Handle MUC message reflection
- Make the storage path configurable in the example and add a warning not to share the data between accounts
- Updated to python-omemo v2.0.0

### Removed
- Removed project.py and simplified version.py as part of the migration towards pyproject.toml

## [1.2.2] - 22nd of October, 2024

### Changed
- Attempt device list download with max_items before falling back to full download

## [1.2.1] - 19th of October, 2024

### Fixed
- Handle groupchat messages in `decrypt_message`

## [1.2.0] - 15th of October, 2024

### Changed
- Drop support for Python3.8, add support for Python3.13, bump PyPy test version to 3.10
- Internal housekeeping, mostly related to pylint

## [1.1.1] - 8th of October, 2024

### Fixed
- Load the twomemo backend even though SCE is not supported
- Treat malformed device list as empty instead of an unrecoverable error
- Try uploading the twomemo bundle again without MAX_ITEMS if that option is not supported

## [1.1.0] - 7th of October, 2024

### Added
- Emit an event when OMEMO has initialized
- Manually subscribe to device list nodes of contacts without working PEP updates (i.e. missing presence subscription)

### Fixed
- Use only strings for data form values used in pubsub publish options and node configuration
- Ignore messages without body in the echo bot example
- Allow passing text content to the echo bot example's `encrypted_reply` method as advertized

## [1.0.0] - 26th of July, 2024

### Added
- Initial release

[Unreleased]: https://github.com/Syndace/slixmpp-omemo/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/Syndace/slixmpp-omemo/compare/v1.2.2...v2.0.0
[1.2.2]: https://github.com/Syndace/slixmpp-omemo/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/Syndace/slixmpp-omemo/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Syndace/slixmpp-omemo/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/Syndace/slixmpp-omemo/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Syndace/slixmpp-omemo/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Syndace/slixmpp-omemo/releases/tag/v1.0.0
