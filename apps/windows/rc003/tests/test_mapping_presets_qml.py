"""Real modal-panel flow in disposable Qt processes; no native remote input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_qt_settings_app as qt_tests


PROBE = r'''
import json, os, time
from pathlib import Path
from PySide6.QtCore import QObject, QMetaObject, QUrl, QPointF, Qt
from PySide6.QtGui import QPalette, QColor, QFontDatabase
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m, config, app_profiles
from tests.test_qt_settings_app import SettingsControllerTests

classes = m._load_qt_classes()
classes['QQuickStyle'].setStyle('FluentWinUI3')
app = classes['QGuiApplication']([])
# The offscreen platform does not enumerate all Windows system font fallbacks.
for font in ('msyh.ttc', 'msyhbd.ttc', 'segmdl2.ttf'):
    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / font
    if path.is_file():
        QFontDatabase.addApplicationFont(str(path))
if os.environ.get('PRESET_TEST_DARK') == '1':
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor('#171a1f'))
    palette.setColor(QPalette.WindowText, QColor('#f3f4f6'))
    app.setPalette(palette)
fixture = SettingsControllerTests()
fixture.setUp()
try:
    profile = os.environ['PRESET_TEST_PROFILE']
    settings = config.load_config(config.config_path())
    settings['selected_device_profile'] = profile
    config.save_settings_pair(config.config_path(), settings, config.key_bindings_path(), config.default_key_bindings(profile))
    controller, model = fixture._make_controller()
    diagnostics = classes['DiagnosticsController'](controller, controller._config_root, auto_refresh=False)
    for key, instance in [('SettingsController', controller), ('ButtonMappingModel', model), ('DiagnosticsController', diagnostics)]:
        classes['qmlRegisterSingletonInstance'](classes[key], 'OvbRc003Settings', 1, 0, key, instance)
    engine = classes['QQmlApplicationEngine']()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(item.toString() for item in items))
    engine.load(QUrl.fromLocalFile(str(m._qml_directory() / 'main.qml')))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    window.setWidth(640)
    window.setHeight(480)
    def tick():
        for _ in range(3):
            app.processEvents()
        QTest.qWait(200)  # Fluent popup transitions finish before the next click.
    def find(name):
        def walk(root):
            if root.objectName() == name:
                return root
            children = list(root.children())
            if hasattr(root, 'childItems'):
                children.extend(child for child in root.childItems() if child not in children)
            for child in children:
                result = walk(child)
                if result is not None:
                    return result
            return None
        obj = walk(window)
        assert obj is not None, name
        return obj
    def invoke(name, method):
        assert QMetaObject.invokeMethod(find(name), method), (name, method)
        tick()
    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
            app.processEvents()
        assert predicate(), 'popup transition did not finish'
    def click(name):
        button = find(name)
        assert button.property('enabled'), name
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        assert 0 <= point.x() <= window.width() and 0 <= point.y() <= window.height(), (name, point)
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point.toPoint())
        tick()
    def click_remove(index):
        # The chip's × only fades in while the chip is hovered.
        chip = find('mappingChip_' + str(index))
        QTest.mouseMove(window, chip.mapToScene(QPointF(chip.width() / 2, chip.height() / 2)).toPoint())
        tick()
        button = find('removeMappingAppButton_' + str(index))
        assert button.property('visible'), 'removeMappingAppButton_' + str(index)
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point.toPoint())
        tick()
    def inside_dialog(item_name, dialog):
        item = find(item_name)
        point = item.mapToScene(QPointF(0, 0))
        assert point.x() >= dialog.property('x'), (item_name, point)
        assert point.x() + item.width() <= dialog.property('x') + dialog.property('width'), item_name

    key_bindings_path = config.key_bindings_path(controller._config_root)
    store = app_profiles.AppProfileStore(controller._config_root)

    find('tabBar').setProperty('currentIndex', 1)
    tick()
    # The application bar starts with just the global chip and "+", and it sits
    # above the button list on the page.
    assert controller.mappingEditTarget == ''
    assert len(controller.mappingAppProfiles) == 0, controller.mappingAppProfiles
    assert find('mappingChip_global').property('objectName') == 'mappingChip_global'
    assert find('mappingChip_global').property('y') < find('mappingList').property('y')

    # "+" on the bar opens the picker; a typed process name is accepted.
    click('addMappingAppButton')
    picker = find('mappingAppPickerDialog')
    wait_for(lambda: picker.property('visible'))
    field = find('mappingAppProcessField')
    inside_dialog('browseMappingAppButton', picker)
    field.setProperty('text', 'chrome.exe')
    tick()
    click('addMappingAppProcessButton')
    assert len(controller.mappingAppProfiles) == 1, controller.mappingAppProfiles
    assert controller.mappingAppProfiles[0]['process'] == 'chrome.exe'
    assert str(controller.mappingAppProfiles[0]['name']).strip(), controller.mappingAppProfiles[0]
    app_id = controller.mappingAppProfiles[0]['id']
    # Adding a second time reuses the same application instead of duplicating it.
    field.setProperty('text', 'Chrome.exe')
    tick()
    click('addMappingAppProcessButton')
    assert len(controller.mappingAppProfiles) == 1, controller.mappingAppProfiles
    assert controller.mappingAppProfiles[0]['id'] == app_id
    # Adding an application retargets the button page, like Logi Options+.
    assert controller.mappingEditTarget == app_id, controller.mappingEditTarget
    invoke('mappingAppPickerDialog', 'reject')
    wait_for(lambda: not picker.property('visible'))

    # The application chip sits after the global one; clicking either chip
    # retargets the button page, and clicking is immediate (no apply step).
    def scene_x(name):
        return find(name).mapToScene(QPointF(0, 0)).x()
    assert scene_x('mappingChip_0') > scene_x('mappingChip_global')
    click('mappingChip_global')
    assert controller.mappingEditTarget == '', controller.mappingEditTarget
    click('mappingChip_0')
    assert controller.mappingEditTarget == app_id, controller.mappingEditTarget

    # The "整体配置" panel is gone; import/export of every layout lives in the
    # 配置档案 manager on the voice page.
    assert find('restoreMappingDefaultsButton') is not None
    assert controller.mappingEditTarget == app_id, controller.mappingEditTarget

    # Editing the button page now writes that application's layout and leaves
    # the global key_bindings.json untouched.
    before_global = json.dumps(config.load_key_bindings(key_bindings_path), sort_keys=True)
    click('editMapping_ok')
    assert find('actionEditorDialog').property('visible')
    find('actionEditorLongCombo').setProperty('editText', '鼠标左键长按')
    click('actionEditorSaveButton')
    def app_saved():
        actions = store.get(app_id)['mapping']['secondary_bindings'].get('ok', {})
        return actions.get('long_press', {}).get('kind') == 'mouse_left_hold'
    wait_for(app_saved)
    assert json.dumps(config.load_key_bindings(key_bindings_path), sort_keys=True) == before_global

    # The redesigned bar has no "in effect" marker any more - it only reflects
    # the layout being edited. Applying a preset still goes through the
    # controller unchanged.
    assert controller.mappingPresetAction('apply', app_id, '')['ok']
    tick()
    assert controller.mappingPresetAction('apply', app_profiles.GLOBAL_ID, '')['ok']
    tick()

    # "移除" on the chip asks for confirmation first; a stray click no longer
    # deletes the application layout.
    click_remove(0)
    confirm = find('removeMappingAppConfirmDialog')
    wait_for(lambda: confirm.property('visible'))
    assert len(controller.mappingAppProfiles) == 1, controller.mappingAppProfiles
    invoke('removeMappingAppConfirmDialog', 'reject')
    wait_for(lambda: not confirm.property('visible'))
    assert len(controller.mappingAppProfiles) == 1, controller.mappingAppProfiles

    # Confirming deletes the application and the page falls back to the global
    # layout.
    click_remove(0)
    wait_for(lambda: confirm.property('visible'))
    invoke('removeMappingAppConfirmDialog', 'accept')
    wait_for(lambda: len(controller.mappingAppProfiles) == 0)
    assert len(controller.mappingAppProfiles) == 0, controller.mappingAppProfiles
    assert controller.mappingEditTarget == ''
    message = find('mappingMessage').property('text')
    assert '已删除' in message, message
    assert not warnings, warnings
    print(json.dumps({'profile': profile, 'warnings': warnings, 'width': 640, 'height': 480}))
    window.hide()
    engine.deleteLater()
    app.processEvents()
finally:
    fixture.tearDown()
    fixture.doCleanups()
    m._shutdown_diagnostics_workers()
'''


@unittest.skipUnless(qt_tests._HAS_PYSIDE6, qt_tests._SKIP_REASON)
class MappingPresetsQmlTests(unittest.TestCase):
    def test_real_panel_flow_in_light_dark_scaled_windows(self):
        for profile, scale, dark in (("xiaomi-rc003", "1", "0"), ("xiaomi-rc003", "1.5", "1")):
            with self.subTest(profile=profile, scale=scale, dark=dark):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", RC003_DISABLE_LIVE_INPUT="1",
                           PRESET_TEST_PROFILE=profile, PRESET_TEST_DARK=dark, QT_SCALE_FACTOR=scale)
                result = subprocess.run([sys.executable, "-c", PROBE], env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["warnings"], [])
