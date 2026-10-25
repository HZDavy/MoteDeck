# Third-party corresponding source

Release status: READY

The Windows binary bundles the open-source Qt for Python 6.11.1 runtime. The
application source is already available in this GPL-3.0-only repository, and the
Qt / PySide6 runtime is used unmodified, so its corresponding source is the
official upstream archive published by The Qt Company. No project-side patch is
applied to either archive.

## Required source archives

### Qt 6.11.1

- File: `qt-everywhere-src-6.11.1.tar.xz`
- Corresponding source: <https://download.qt.io/official_releases/qt/6.11/6.11.1/single/qt-everywhere-src-6.11.1.tar.xz>
- Size: 1,017,723,080 bytes
- SHA-256: `252acef8c5ae68074d91cadba2ee4a83465051bbb970dd26e8f0daa0f3904e03`

### Qt for Python / PySide6 6.11.1

- File: `pyside-setup-everywhere-src-6.11.1.tar.xz`
- Corresponding source: <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz>
- Size: 17,963,432 bytes
- SHA-256: `6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2`

## Distribution statement

The Windows build installs the official `PySide6-Essentials==6.11.1` and
`shiboken6==6.11.1` PyPI wheels and does not patch their source. PyInstaller
keeps Qt as replaceable DLLs and QML/plugin files under the application's
`程序文件/PySide6` directory rather than statically linking them into the main
executable.

Corresponding-source delivery for the Windows binary:

1. both archives above are the unmodified upstream releases, available for free
   from The Qt Company's official download server at permanently published URLs;
2. the sizes and SHA-256 values recorded above identify the exact archives the
   binary was built against;
3. anyone redistributing this binary may keep those URLs as the
   corresponding-source location, or mirror the same archives unchanged and keep
   the recorded hashes verifiable.

No build-side modification of Qt, PySide6 or Shiboken is performed by this
project, so no project-specific source patch has to be published alongside them.
