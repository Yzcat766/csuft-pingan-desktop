# License text sources

- Qt for Python 6.12.0: [LGPL-3.0-only](https://github.com/pyside/pyside-setup/blob/v6.12.0/LICENSES/LGPL-3.0-only.txt), [GPL-3.0-only](https://github.com/pyside/pyside-setup/blob/v6.12.0/LICENSES/GPL-3.0-only.txt), [GPL-2.0-only](https://github.com/pyside/pyside-setup/blob/v6.12.0/LICENSES/GPL-2.0-only.txt).
- Python 3.13.13 (local build interpreter): [LICENSE](https://github.com/python/cpython/blob/v3.13.13/LICENSE). CI may use a later Python 3.13 patch; its interpreter license is additionally collected during packaging when available.
- [Qt licensing overview](https://doc.qt.io/qt-6/licensing.html) and [Qt for Python](https://doc.qt.io/qtforpython-6/).

Corresponding source: [Qt for Python v6.12.0](https://github.com/pyside/pyside-setup/tree/v6.12.0), [Qt 6.12.0 source archives](https://download.qt.io/archive/qt/6.12/6.12.0/single/), [CPython v3.13.13](https://github.com/python/cpython/tree/v3.13.13). Package metadata identifies the installed PySide6 as LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only. Its bundled commercial alternative notice is preserved separately without selecting that license.

The application uses Qt Core, Gui and Widgets and dynamically loads the bundled Qt/PySide libraries. The application source and build instructions permit rebuilding with compatible modified libraries; no restriction on debugging such modifications is added by this project. Other collected dependency notices accompany each Windows portable archive.
