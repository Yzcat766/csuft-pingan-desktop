# 第三方许可与来源

本项目由原本机 Python 桌面工程整理而来，业务逻辑源自 [HeWhenJay/csuft-pingan-daka](https://github.com/HeWhenJay/csuft-pingan-daka)。沿用原工程的 MIT 许可证，完整版权与授权条款保留于 `LICENSE`。原文件中的 `csuft-unsafe-dorm contributors` 版权归属未被替换。

Windows 发行版由 PyInstaller 打包，并包含 Python 及以下主要依赖。下表是许可导航，不替代依赖自带的完整条款。打包时收集到的许可文本随便携 ZIP 的 `third-party-licenses/` 一并分发。

| 组件 | 许可 |
| --- | --- |
| Python | PSF License 及其随附第三方声明 |
| PySide6 / Shiboken6 / Qt | LGPLv3 / GPL / 商业许可，具体按模块随附条款 |
| Requests | Apache License 2.0 |
| cryptography | Apache License 2.0 或 BSD，及随附 OpenSSL 等条款 |
| pywin32 | PSF 派生许可 |
| PyInstaller bootloader | GPL 及其允许分发打包应用的例外 |

PyInstaller 单文件程序会在运行时将动态库解压到临时目录。完整项目源码、依赖版本及构建脚本在本仓库中公开，便于自行重建和使用兼容的依赖版本。第三方组件的商标与版权归各自权利人所有。
