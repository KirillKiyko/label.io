import sys
import codecs
import os.path
import resources
import subprocess

from sys import platform
from PyQt4.QtGui import *
from PyQt4.QtCore import *
from libs.ustr import ustr
from libs.constants import *
from functools import partial
from libs.canvas import Canvas
from libs.toolBar import ToolBar
from libs.settings import Settings
from libs.zoomWidget import ZoomWidget
from libs.pascal_voc_io import XML_EXT
from libs.labelDialog import LabelDialog
from libs.colorDialog import ColorDialog
from libs.pascal_voc_io import PascalVocReader
from libs.labelFile import LabelFile, LabelFileError
from libs.shape import Shape, DEFAULT_LINE_COLOR, DEFAULT_FILL_COLOR
from libs.lib import struct, newAction, newIcon, addActions, fmtShortcut

__appname__ = 'label.io'


def have_qstring():
    return not (sys.version_info.major >= 3 or QT_VERSION_STR.startswith('5.'))


def util_qt_strlistclass():
    return QStringList if have_qstring() else list


class WindowMixin(object):
    def menu(self, title, actions=None):
        menu = self.menuBar().addMenu(title)

        if actions:
            addActions(menu, actions)

        return menu

    def toolbar(self, title, actions=None):
        toolbar = ToolBar(title)
        toolbar.setObjectName(u'%sToolBar' % title)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)

        if actions:
            addActions(toolbar, actions)

        self.addToolBar(Qt.LeftToolBarArea, toolbar)

        return toolbar


class HashableQListWidgetItem(QListWidgetItem):
    def __init__(self, *args):
        super(HashableQListWidgetItem, self).__init__(*args)

    def __hash__(self):
        return hash(id(self))


class MainWindow(QMainWindow, WindowMixin):
    FIT_WINDOW, FIT_WIDTH, MANUAL_ZOOM = list(range(3))

    def __init__(self, defaultFilename=None, defaultPrefdefClassFile=None):
        super(MainWindow, self).__init__()

        self.setWindowTitle(__appname__)
        self.defaultSaveDir = None
        self.usingPascalVocFormat = True
        self.mImgList = []
        self.dirname = None
        self.labelHist = []
        self.lastOpenDir = None
        self.dirty = False
        self._noSelectionSlot = False
        self._beginner = True

        self.screencastViewer = "firefox"
        self.screencast = "https://www.youtube.com/?gl=UA&hl=ru"

        self.loadPredefinedClasses(defaultPrefdefClassFile)
        self.labelDialog = LabelDialog(parent=self, listItem=self.labelHist)

        self.itemsToShapes = {}
        self.shapesToItems = {}
        self.prevLabelText = ''

        listLayout = QVBoxLayout()
        listLayout.setContentsMargins(0, 0, 0, 0)

        self.useDefaultLabelCheckbox = QCheckBox(u'Default label')
        self.useDefaultLabelCheckbox.setChecked(True)
        self.defaultLabelTextLine = QLineEdit('Object')

        useDefaultLabelQHBoxLayout = QHBoxLayout()
        useDefaultLabelQHBoxLayout.addWidget(self.useDefaultLabelCheckbox)
        useDefaultLabelQHBoxLayout.addWidget(self.defaultLabelTextLine)
        useDefaultLabelContainer = QWidget()
        useDefaultLabelContainer.setLayout(useDefaultLabelQHBoxLayout)

        self.editButton = QToolButton()
        self.editButton.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        listLayout.addWidget(self.editButton)
        listLayout.addWidget(useDefaultLabelContainer)

        self.labelList = QListWidget()

        labelListContainer = QWidget()
        labelListContainer.setLayout(listLayout)

        self.labelList.itemActivated.connect(self.labelSelectionChanged)
        self.labelList.itemSelectionChanged.connect(self.labelSelectionChanged)
        self.labelList.itemDoubleClicked.connect(self.editLabel)

        self.labelList.itemChanged.connect(self.labelItemChanged)

        listLayout.addWidget(self.labelList)

        self.dock = QDockWidget(u'Label List', self)
        self.dock.setObjectName(u'Labels')
        self.dock.setWidget(labelListContainer)

        self.fileListWidget = QListWidget()
        self.fileListWidget.itemDoubleClicked.connect(self.fileitemDoubleClicked)

        filelistLayout = QVBoxLayout()
        filelistLayout.setContentsMargins(0, 0, 0, 0)
        filelistLayout.addWidget(self.fileListWidget)
        fileListContainer = QWidget()
        fileListContainer.setLayout(filelistLayout)

        self.filedock = QDockWidget(u'File List', self)
        self.filedock.setObjectName(u'Files')
        self.filedock.setWidget(fileListContainer)

        self.zoomWidget = ZoomWidget()
        self.colorDialog = ColorDialog(parent=self)

        self.canvas = Canvas()
        self.canvas.zoomRequest.connect(self.zoomRequest)

        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(True)

        self.scrollBars = {Qt.Vertical: scroll.verticalScrollBar(), Qt.Horizontal: scroll.horizontalScrollBar()}
        self.scrollArea = scroll
        self.canvas.scrollRequest.connect(self.scrollRequest)

        self.canvas.newShape.connect(self.newShape)
        self.canvas.shapeMoved.connect(self.setDirty)
        self.canvas.selectionChanged.connect(self.shapeSelectionChanged)
        self.canvas.drawingPolygon.connect(self.toggleDrawingSensitive)

        self.setCentralWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)

        self.addDockWidget(Qt.RightDockWidgetArea, self.filedock)
        self.dockFeatures = QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetFloatable
        self.dock.setFeatures(self.dock.features() ^ self.dockFeatures)

        action = partial(newAction, self)

        open = action('&Open Image', self.openFile,
                      'I', 'open', u'Open image file')

        opendir = action('&Open Folder', self.openDir,
                         'F', 'open', u'Open directory')

        openNextImg = action('&Next Image', self.openNextImg,
                             'D', 'next', u'Open next image')

        openPrevImg = action('&Previous Image', self.openPrevImg,
                             'A', 'prev', u'Open previous image')

        save = action('&Save Label', self.saveFile,
                      'S', 'save', u'Save labels to file', enabled=False)

        createMode = action('Create Label', self.setCreateMode,
                            'W', 'new', u'Create new label', enabled=False)

        editMode = action('&Edit Label', self.setEditMode,
                          'E', 'edit', u'Move and edit label', enabled=False)

        delete = action('Delete label', self.deleteSelectedShape,
                        'Delete', 'delete', u'Delete label', enabled=False)

        advancedMode = action('&Advanced Mode', self.toggleAdvancedMode,
                              'Ctrl+Shift+A', 'expert', u'Switch to advanced mode',
                              checkable=True)

        help = action('&Tutorial', self.tutorial, 'T', 'help',
                      u'Show demo video')

        zoom = QWidgetAction(self)
        zoom.setDefaultWidget(self.zoomWidget)

        self.zoomWidget.setWhatsThis(
            u"Zoom in or out of the image. Also accessible with"
            " %s and %s from the canvas." % (fmtShortcut("Ctrl+[-+]"),
                                             fmtShortcut("Ctrl+Wheel")))

        self.zoomWidget.setEnabled(False)

        zoomIn = action('Zoom &In', partial(self.addZoom, 10),
                        'Ctrl++', 'zoom-in', u'Increase zoom level', enabled=False)

        zoomOut = action('&Zoom Out', partial(self.addZoom, -10),
                         'Ctrl+-', 'zoom-out', u'Decrease zoom level', enabled=False)

        zoomOrg = action('&Original size', partial(self.setZoom, 100),
                         'Ctrl+=', 'zoom', u'Zoom to original size', enabled=False)

        fitWindow = action('&Fit Window', self.setFitWindow,
                           'Ctrl+F', 'fit-window', u'Zoom follows window size',
                           checkable=True, enabled=False)

        fitWidth = action('Fit &Width', self.setFitWidth,
                          'Ctrl+Shift+F', 'fit-width', u'Zoom follows window width',
                          checkable=True, enabled=False)

        zoomActions = (self.zoomWidget, zoomIn, zoomOut, zoomOrg, fitWindow, fitWidth)

        self.zoomMode = self.MANUAL_ZOOM
        self.scalers = {self.FIT_WINDOW: self.scaleFitWindow,
                        self.FIT_WIDTH: self.scaleFitWidth,
                        self.MANUAL_ZOOM: lambda: 1,}

        edit = action('&Edit Label', self.editLabel,
                      'Ctrl+E', 'edit', u'Modify the label of the selected Box',
                      enabled=False)

        self.editButton.setDefaultAction(edit)

        shapeLineColor = action('Shape &Line Color', self.chshapeLineColor,
                                icon='color_line', tip=u'Change the line color for this specific shape',
                                enabled=False)

        shapeFillColor = action('Shape &Fill Color', self.chshapeFillColor,
                                icon='color', tip=u'Change the fill color for this specific shape',
                                enabled=False)

        labels = self.dock.toggleViewAction()

        labels.setText('Label Panel')
        labels.setShortcut('Ctrl+Shift+L')

        files = self.filedock.toggleViewAction()
        files.setText('File Panel')
        files.setShortcut('Ctrl+Shift+F')


        labelMenu = QMenu()
        addActions(labelMenu, (edit, delete))

        self.labelList.setContextMenuPolicy(Qt.CustomContextMenu)
        self.labelList.customContextMenuRequested.connect(self.popLabelListMenu)

        self.actions = struct(save=save,
                              open=open,
                              create=createMode,
                              delete=delete,
                              edit=edit,
                              createMode=createMode,
                              editMode=editMode,
                              advancedMode=advancedMode,
                              shapeLineColor=shapeLineColor,
                              shapeFillColor=shapeFillColor,
                              zoom=zoom,
                              zoomIn=zoomIn,
                              zoomOut=zoomOut,
                              zoomOrg=zoomOrg,
                              fitWindow=fitWindow,
                              fitWidth=fitWidth,
                              zoomActions=zoomActions,
                              fileMenuActions=(open, opendir, save),
                              beginner=(),
                              advanced=(),
                              editMenu=(edit, delete),
                              beginnerContext=(edit, delete),
                              advancedContext=(save, createMode, editMode),
                              onLoadActive=(createMode, editMode))

        self.menus = struct(file=self.menu('&File'),
                            edit=self.menu('&Edit'),
                            view=self.menu('&View'),
                            help=self.menu('&Help'),
                            recentFiles=QMenu('Open &Recent'),
                            labelList=labelMenu)

        self.autoSaving = QAction("Auto Saving", self)
        self.autoSaving.setCheckable(True)

        self.singleClassMode = QAction("Single Class Mode", self)
        self.singleClassMode.setShortcut("Ctrl+Shift+S")
        self.singleClassMode.setCheckable(True)
        self.lastLabel = None

        addActions(self.menus.file, (open, opendir, openNextImg, openPrevImg))
        addActions(self.menus.help, (help,))
        addActions(self.menus.view, (labels, files, zoomIn, zoomOut))

        self.menus.file.aboutToShow.connect(self.updateFileMenu)

        addActions(self.canvas.menus[0], self.actions.beginnerContext)
        addActions(self.canvas.menus[1], (action('&Copy here', self.copyShape), action('&Move here', self.moveShape)))

        self.tools = self.toolbar('Tools')

        self.actions.advanced = (None, open, opendir, None, openNextImg, openPrevImg, None, save, createMode,
                                 editMode, None, zoomIn, zoomOut, None)

        self.statusBar().showMessage('%s started.' % __appname__)
        self.statusBar().show()

        self.image = QImage()
        self.filePath = ustr(defaultFilename)
        self.recentFiles = []
        self.maxRecent = 7
        self.lineColor = None
        self.fillColor = None
        self.zoom_level = 100
        self.fit_window = False
        self.difficult = False

        self.settings = Settings()
        self.settings.load()

        settings = self.settings

        if settings.get(SETTING_RECENT_FILES):
            if have_qstring():
                recentFileQStringList = settings.get(SETTING_RECENT_FILES)
                self.recentFiles = [ustr(i) for i in recentFileQStringList]
            else:
                self.recentFiles = recentFileQStringList = settings.get(SETTING_RECENT_FILES)

        size = settings.get(SETTING_WIN_SIZE, QSize(300, 500))
        position = settings.get(SETTING_WIN_POSE, QPoint(0, 0))

        self.resize(size)
        self.move(position)

        saveDir = ustr(settings.get(SETTING_SAVE_DIR, None))

        self.lastOpenDir = ustr(settings.get(SETTING_LAST_OPEN_DIR, None))

        if saveDir is not None and os.path.exists(saveDir):
            self.defaultSaveDir = saveDir
            self.statusBar().showMessage('%s started. Annotation will be saved to %s' %
                                         (__appname__, self.defaultSaveDir))
            self.statusBar().show()

        self.restoreState(settings.get(SETTING_WIN_STATE, QByteArray()))
        self.lineColor = QColor(settings.get(SETTING_LINE_COLOR, Shape.line_color))
        self.fillColor = QColor(settings.get(SETTING_FILL_COLOR, Shape.fill_color))

        Shape.line_color = self.lineColor
        Shape.fill_color = self.fillColor
        Shape.difficult = self.difficult

        def xbool(x):
            if isinstance(x, QVariant):
                return x.toBool()

            return bool(x)

        if xbool(settings.get(SETTING_ADVANCE_MODE, False)):
            self.actions.advancedMode.setChecked(True)
            self.toggleAdvancedMode()

        self.updateFileMenu()
        self.queueEvent(partial(self.loadFile, self.filePath or ""))
        self.zoomWidget.valueChanged.connect(self.paintCanvas)
        self.populateModeActions()

    def noShapes(self):
        return not self.itemsToShapes

    def toggleAdvancedMode(self, value=True):
        self._beginner = not value
        self.canvas.setEditing(True)
        self.populateModeActions()
        self.editButton.setVisible(not value)

        if value:
            self.actions.createMode.setEnabled(True)
            self.actions.editMode.setEnabled(False)
            self.dock.setFeatures(self.dock.features() | self.dockFeatures)
        else:
            self.dock.setFeatures(self.dock.features() ^ self.dockFeatures)

    def populateModeActions(self):
        tool, menu = self.actions.advanced, self.actions.advancedContext

        self.tools.clear()
        addActions(self.tools, tool)
        self.canvas.menus[0].clear()
        addActions(self.canvas.menus[0], menu)
        self.menus.edit.clear()
        actions = (self.actions.create,) if self.beginner() else (self.actions.createMode, self.actions.editMode)
        addActions(self.menus.edit, actions + self.actions.advancedContext)

    def setBeginner(self):
        self.tools.clear()
        addActions(self.tools, self.actions.beginner)

    def setAdvanced(self):
        self.tools.clear()
        addActions(self.tools, self.actions.advanced)

    def setDirty(self):
        self.dirty = True
        self.actions.save.setEnabled(True)

    def setClean(self):
        self.dirty = False
        self.actions.save.setEnabled(False)

    def toggleActions(self, value=True):
        for z in self.actions.zoomActions:
            z.setEnabled(value)

        for action in self.actions.onLoadActive:
            action.setEnabled(value)

    def queueEvent(self, function):
        QTimer.singleShot(0, function)

    def status(self, message, delay=5000):
        self.statusBar().showMessage(message, delay)

    def resetState(self):
        self.itemsToShapes.clear()
        self.shapesToItems.clear()
        self.labelList.clear()
        self.filePath = None
        self.imageData = None
        self.labelFile = None
        self.canvas.resetState()

    def currentItem(self):
        items = self.labelList.selectedItems()

        if items:
            return items[0]

        return None

    def addRecentFile(self, filePath):
        if filePath in self.recentFiles:
            self.recentFiles.remove(filePath)
        elif len(self.recentFiles) >= self.maxRecent:
            self.recentFiles.pop()

        self.recentFiles.insert(0, filePath)

    def beginner(self):
        return self._beginner

    def advanced(self):
        return not self.beginner()

    def tutorial(self):
        subprocess.Popen([self.screencastViewer, self.screencast])

    def createShape(self):
        assert self.beginner()
        self.canvas.setEditing(False)
        self.actions.create.setEnabled(False)

    def toggleDrawingSensitive(self, drawing=True):
        self.actions.editMode.setEnabled(not drawing)

        if not drawing and self.beginner():
            print('Cancel creation.')
            self.canvas.setEditing(True)
            self.canvas.restoreCursor()
            self.actions.create.setEnabled(True)

    def toggleDrawMode(self, edit=True):
        self.canvas.setEditing(edit)
        self.actions.createMode.setEnabled(edit)
        self.actions.editMode.setEnabled(not edit)

    def setCreateMode(self):
        # assert self.advanced()
        self.toggleDrawMode(False)

    def setEditMode(self):
        assert self.advanced()
        self.toggleDrawMode(True)
        self.labelSelectionChanged()

    def updateFileMenu(self):
        currFilePath = self.filePath

        def exists(filename):
            return os.path.exists(filename)

        menu = self.menus.recentFiles
        menu.clear()
        files = [f for f in self.recentFiles if f != currFilePath and exists(f)]

        for i, f in enumerate(files):
            icon = newIcon('labels')
            action = QAction(icon, '&%d %s' % (i + 1, QFileInfo(f).fileName()), self)
            action.triggered.connect(partial(self.loadRecent, f))
            menu.addAction(action)

    def popLabelListMenu(self, point):
        self.menus.labelList.exec_(self.labelList.mapToGlobal(point))

    def editLabel(self, item=None):
        if not self.canvas.editing():
            return

        item = item if item else self.currentItem()
        text = self.labelDialog.popUp(item.text())

        if text is not None:
            item.setText(text)
            self.setDirty()

    def fileitemDoubleClicked(self, item=None):
        currIndex = self.mImgList.index(ustr(item.text()))

        if currIndex < len(self.mImgList):
            filename = self.mImgList[currIndex]

            if filename:
                self.loadFile(filename)

    def btnstate(self, item=None):
        if not self.canvas.editing():
            return

        item = self.currentItem()

        if not item:
            item = self.labelList.item(self.labelList.count() - 1)

        try:
            shape = self.itemsToShapes[item]
        except:
            pass

    def shapeSelectionChanged(self, selected=False):
        if self._noSelectionSlot:
            self._noSelectionSlot = False
        else:
            shape = self.canvas.selectedShape

            if shape:
                self.shapesToItems[shape].setSelected(True)
            else:
                self.labelList.clearSelection()

        self.actions.delete.setEnabled(selected)
        self.actions.copy.setEnabled(selected)
        self.actions.edit.setEnabled(selected)
        self.actions.shapeLineColor.setEnabled(selected)
        self.actions.shapeFillColor.setEnabled(selected)

    def addLabel(self, shape):
        item = HashableQListWidgetItem(shape.label)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)

        self.itemsToShapes[item] = shape
        self.shapesToItems[shape] = item
        self.labelList.addItem(item)

    def remLabel(self, shape):
        if shape is None:
            return

        item = self.shapesToItems[shape]
        self.labelList.takeItem(self.labelList.row(item))

        del self.shapesToItems[shape]
        del self.itemsToShapes[item]

    def loadLabels(self, shapes):
        s = []

        for label, points, line_color, fill_color, difficult in shapes:
            shape = Shape(label=label)

            for x, y in points:
                shape.addPoint(QPointF(x, y))

            shape.difficult = difficult
            shape.close()
            s.append(shape)

            self.addLabel(shape)

            if line_color:
                shape.line_color = QColor(*line_color)

            if fill_color:
                shape.fill_color = QColor(*fill_color)

        self.canvas.loadShapes(s)

    def saveLabels(self, annotationFilePath):
        annotationFilePath = ustr(annotationFilePath)

        if self.labelFile is None:
            self.labelFile = LabelFile()
            self.labelFile.verified = self.canvas.verified

        def format_shape(s):
            return dict(label=s.label,
                        line_color=s.line_color.getRgb()
                        if s.line_color != self.lineColor else None,
                        fill_color=s.fill_color.getRgb()
                        if s.fill_color != self.fillColor else None,
                        points=[(p.x(), p.y()) for p in s.points],
                        difficult=s.difficult)

        shapes = [format_shape(shape) for shape in self.canvas.shapes]

        try:
            if self.usingPascalVocFormat is True:
                print ('Img: ' + self.filePath + ' -> Its xml: ' + annotationFilePath)
                self.labelFile.savePascalVocFormat(annotationFilePath,
                                                   shapes,
                                                   self.filePath,
                                                   self.imageData,
                                                   self.lineColor.getRgb(),
                                                   self.fillColor.getRgb())
            else:
                self.labelFile.save(annotationFilePath,
                                    shapes,
                                    self.filePath,
                                    self.imageData,
                                    self.lineColor.getRgb(),
                                    self.fillColor.getRgb())

            return True
        except LabelFileError as e:
            self.errorMessage(u'Error saving label data', u'<b>%s</b>' % e)

            return False

    def copySelectedShape(self):
        self.addLabel(self.canvas.copySelectedShape())
        self.shapeSelectionChanged(True)

    def labelSelectionChanged(self):
        item = self.currentItem()

        if item and self.canvas.editing():
            self._noSelectionSlot = True
            self.canvas.selectShape(self.itemsToShapes[item])
            shape = self.itemsToShapes[item]

    def labelItemChanged(self, item):
        shape = self.itemsToShapes[item]
        label = item.text()

        if label != shape.label:
            shape.label = item.text()
            self.setDirty()
        else:
            self.canvas.setShapeVisible(shape, item.checkState() == Qt.Checked)

    def newShape(self):
        if not self.useDefaultLabelCheckbox.isChecked() or not self.defaultLabelTextLine.text():
            if len(self.labelHist) > 0:
                self.labelDialog = LabelDialog(
                    parent=self, listItem=self.labelHist)

            if self.singleClassMode.isChecked() and self.lastLabel:
                text = self.lastLabel
            else:
                text = self.labelDialog.popUp(text=self.prevLabelText)
                self.lastLabel = text
        else:
            text = self.defaultLabelTextLine.text()

        if text is not None:
            self.prevLabelText = text
            self.addLabel(self.canvas.setLastLabel(text))

            if self.beginner():
                self.canvas.setEditing(True)
                self.actions.create.setEnabled(True)
            else:
                self.actions.editMode.setEnabled(True)
            self.setDirty()

            if text not in self.labelHist:
                self.labelHist.append(text)
        else:
            self.canvas.resetAllLines()

    def scrollRequest(self, delta, orientation):
        units = - delta / (8 * 15)
        bar = self.scrollBars[orientation]
        bar.setValue(bar.value() + bar.singleStep() * units)

    def setZoom(self, value):
        self.actions.fitWidth.setChecked(False)
        self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.MANUAL_ZOOM
        self.zoomWidget.setValue(value)

    def addZoom(self, increment=10):
        self.setZoom(self.zoomWidget.value() + increment)

    def zoomRequest(self, delta):
        h_bar = self.scrollBars[Qt.Horizontal]
        v_bar = self.scrollBars[Qt.Vertical]

        h_bar_max = h_bar.maximum()
        v_bar_max = v_bar.maximum()

        cursor = QCursor()
        pos = cursor.pos()
        relative_pos = QWidget.mapFromGlobal(self, pos)

        cursor_x = relative_pos.x()
        cursor_y = relative_pos.y()

        w = self.scrollArea.width()
        h = self.scrollArea.height()

        margin = 0.1
        move_x = (cursor_x - margin * w) / (w - 2 * margin * w)
        move_y = (cursor_y - margin * h) / (h - 2 * margin * h)

        move_x = min(max(move_x, 0), 1)
        move_y = min(max(move_y, 0), 1)

        units = delta / (8 * 15)
        scale = 10

        self.addZoom(scale * units)

        d_h_bar_max = h_bar.maximum() - h_bar_max
        d_v_bar_max = v_bar.maximum() - v_bar_max

        new_h_bar_value = h_bar.value() + move_x * d_h_bar_max
        new_v_bar_value = v_bar.value() + move_y * d_v_bar_max

        h_bar.setValue(new_h_bar_value)
        v_bar.setValue(new_v_bar_value)

    def setFitWindow(self, value=True):
        if value:
            self.actions.fitWidth.setChecked(False)
        self.zoomMode = self.FIT_WINDOW if value else self.MANUAL_ZOOM
        self.adjustScale()

    def setFitWidth(self, value=True):
        if value:
            self.actions.fitWindow.setChecked(False)
        self.zoomMode = self.FIT_WIDTH if value else self.MANUAL_ZOOM
        self.adjustScale()

    def togglePolygons(self, value):
        for item, shape in self.itemsToShapes.items():
            item.setCheckState(Qt.Checked if value else Qt.Unchecked)

    def loadFile(self, filePath=None):
        self.resetState()
        self.canvas.setEnabled(False)

        if filePath is None:
            filePath = self.settings.get(SETTING_FILENAME)

        unicodeFilePath = ustr(filePath)

        if unicodeFilePath and self.fileListWidget.count() > 0:
            index = self.mImgList.index(unicodeFilePath)
            fileWidgetItem = self.fileListWidget.item(index)
            fileWidgetItem.setSelected(True)

        if unicodeFilePath and os.path.exists(unicodeFilePath):
            if LabelFile.isLabelFile(unicodeFilePath):
                try:
                    self.labelFile = LabelFile(unicodeFilePath)
                except LabelFileError as e:
                    self.errorMessage(u'Error opening file',
                                      (u"<p><b>%s</b></p>"
                                       u"<p>Make sure <i>%s</i> is a valid label file.")
                                      % (e, unicodeFilePath))
                    self.status("Error reading %s" % unicodeFilePath)
                    return False
                self.imageData = self.labelFile.imageData
                self.lineColor = QColor(*self.labelFile.lineColor)
                self.fillColor = QColor(*self.labelFile.fillColor)
            else:
                self.imageData = read(unicodeFilePath, None)
                self.labelFile = None

            image = QImage.fromData(self.imageData)

            if image.isNull():
                self.errorMessage(u'Error opening file',
                                  u"<p>Make sure <i>%s</i> is a valid image file." % unicodeFilePath)
                self.status("Error reading %s" % unicodeFilePath)

                return False

            self.status("Loaded %s" % os.path.basename(unicodeFilePath))
            self.image = image
            self.filePath = unicodeFilePath
            self.canvas.loadPixmap(QPixmap.fromImage(image))

            if self.labelFile:
                self.loadLabels(self.labelFile.shapes)

            self.setClean()
            self.canvas.setEnabled(True)
            self.adjustScale(initial=True)
            self.paintCanvas()
            self.addRecentFile(self.filePath)
            self.toggleActions(True)

            if self.usingPascalVocFormat is True:
                if self.defaultSaveDir is not None:
                    basename = os.path.basename(
                        os.path.splitext(self.filePath)[0]) + XML_EXT
                    xmlPath = os.path.join(self.defaultSaveDir, basename)
                    self.loadPascalXMLByFilename(xmlPath)
                else:
                    xmlPath = os.path.splitext(filePath)[0] + XML_EXT

                    if os.path.isfile(xmlPath):
                        self.loadPascalXMLByFilename(xmlPath)

            self.setWindowTitle(__appname__ + ' ' + filePath)

            if self.labelList.count():
                self.labelList.setCurrentItem(self.labelList.item(self.labelList.count() - 1))
                self.labelList.item(self.labelList.count() - 1).setSelected(True)

            self.canvas.setFocus(True)

            return True

        return False

    def resizeEvent(self, event):
        if self.canvas and not self.image.isNull() and self.zoomMode != self.MANUAL_ZOOM:
            self.adjustScale()

        super(MainWindow, self).resizeEvent(event)

    def paintCanvas(self):
        assert not self.image.isNull(), "cannot paint null image"

        self.canvas.scale = 0.01 * self.zoomWidget.value()
        self.canvas.adjustSize()
        self.canvas.update()

    def adjustScale(self, initial=False):
        value = self.scalers[self.FIT_WINDOW if initial else self.zoomMode]()
        self.zoomWidget.setValue(int(100 * value))

    def scaleFitWindow(self):
        e = 2.0

        w1 = self.centralWidget().width() - e
        h1 = self.centralWidget().height() - e

        a1 = w1 / h1

        w2 = self.canvas.pixmap.width() - 0.0
        h2 = self.canvas.pixmap.height() - 0.0

        a2 = w2 / h2

        return w1 / w2 if a2 >= a1 else h1 / h2

    def scaleFitWidth(self):
        w = self.centralWidget().width() - 2.0

        return w / self.canvas.pixmap.width()

    def closeEvent(self, event):
        if not self.mayContinue():
            event.ignore()

        settings = self.settings

        if self.dirname is None:
            settings[SETTING_FILENAME] = self.filePath if self.filePath else ''
        else:
            settings[SETTING_FILENAME] = ''

        settings[SETTING_WIN_SIZE] = self.size()
        settings[SETTING_WIN_POSE] = self.pos()
        settings[SETTING_WIN_STATE] = self.saveState()
        settings[SETTING_LINE_COLOR] = self.lineColor
        settings[SETTING_FILL_COLOR] = self.fillColor
        settings[SETTING_RECENT_FILES] = self.recentFiles
        settings[SETTING_ADVANCE_MODE] = not self._beginner

        if self.defaultSaveDir is not None and len(self.defaultSaveDir) > 1:
            settings[SETTING_SAVE_DIR] = ustr(self.defaultSaveDir)
        else:
            settings[SETTING_SAVE_DIR] = ""

        if self.lastOpenDir is not None and len(self.lastOpenDir) > 1:
            settings[SETTING_LAST_OPEN_DIR] = self.lastOpenDir
        else:
            settings[SETTING_LAST_OPEN_DIR] = ""

        settings.save()

    def loadRecent(self, filename):
        if self.mayContinue():
            self.loadFile(filename)

    def scanAllImages(self, folderPath):
        extensions = ['.jpg', '.png']
        images = []

        for root, dirs, files in os.walk(folderPath):
            for file in files:
                if file.lower().endswith(tuple(extensions)):
                    relativePath = os.path.join(root, file)
                    path = ustr(os.path.abspath(relativePath))
                    images.append(path)

        try:
            path = os.path.split(images[0])[0]
            extention = os.path.splitext(images[0])[1]
            images = [int(os.path.splitext(os.path.basename(i))[0]) for i in images]
            images = sorted(images)
            if platform == 'win32' or platform == 'win64':
                images = [path + '\\' + str(i) + extention for i in images]
            else:
                images = [path + '/' + str(i) + extention for i in images]
        except:
            images.sort(key=lambda x: x.lower())

        return images

    def openAnnotation(self, _value=False):
        if self.filePath is None:
            self.statusBar().showMessage('Please select image first')
            self.statusBar().show()

            return

        path = os.path.dirname(ustr(self.filePath)) \
            if self.filePath else '.'

        if self.usingPascalVocFormat:
            filters = "Open Annotation XML file (%s)" % ' '.join(['*.xml'])
            filename = ustr(QFileDialog.getOpenFileName(self, '%s - Choose a xml file' % __appname__, path, filters))

            if filename:
                if isinstance(filename, (tuple, list)):
                    filename = filename[0]

            self.loadPascalXMLByFilename(filename)

    def openDir(self, _value=False):
        if not self.mayContinue():
            return

        path = os.path.dirname(self.filePath) \
            if self.filePath else '.'

        if self.lastOpenDir is not None and len(self.lastOpenDir) > 1:
            path = self.lastOpenDir

        dirpath = ustr(QFileDialog.getExistingDirectory(self,
                                                        '%s - Open Directory' % __appname__, path,
                                                        QFileDialog.ShowDirsOnly
                                                        | QFileDialog.DontResolveSymlinks))

        if dirpath is not None and len(dirpath) > 1:
            self.lastOpenDir = dirpath

        self.dirname = dirpath
        self.filePath = None
        self.fileListWidget.clear()
        self.mImgList = self.scanAllImages(dirpath)
        self.openNextImg()

        for imgPath in self.mImgList:
            item = QListWidgetItem(imgPath)
            self.fileListWidget.addItem(item)

    def openPrevImg(self, _value=False):
        if self.autoSaving.isChecked():
            if self.defaultSaveDir is not None:
                if self.dirty is True:
                    self.saveFile()

        if not self.mayContinue():
            return

        if len(self.mImgList) <= 0:
            return

        if self.filePath is None:
            return

        currIndex = self.mImgList.index(self.filePath)

        if currIndex - 1 >= 0:
            filename = self.mImgList[currIndex - 1]

            if filename:
                self.loadFile(filename)

    def openNextImg(self, _value=False):
        if self.autoSaving.isChecked():
            if self.defaultSaveDir is not None:
                if self.dirty is True:
                    self.saveFile()

        if not self.mayContinue():
            return

        if len(self.mImgList) <= 0:
            return

        filename = None

        if self.filePath is None:
            filename = self.mImgList[0]
        else:
            currIndex = self.mImgList.index(self.filePath)

            if currIndex + 1 < len(self.mImgList):
                filename = self.mImgList[currIndex + 1]

        if filename:
            self.loadFile(filename)

    def openFile(self, _value=False):
        if not self.mayContinue():
            return
        path = os.path.dirname(ustr(self.filePath)) if self.filePath else '.'
        formats = ['*.%s' % fmt.data().decode("ascii").lower() for fmt in QImageReader.supportedImageFormats()]
        filters = "Image & Label files (%s)" % ' '.join(formats + ['*%s' % LabelFile.suffix])
        filename = QFileDialog.getOpenFileName(self, '%s - Choose Image or Label file' % __appname__, path, filters)

        if filename:
            if isinstance(filename, (tuple, list)):
                filename = filename[0]

            try:
                self.loadFile(filename)
            except:
                pass

    def saveFile(self, _value=False):
        imgFileDir = os.path.dirname(self.filePath)
        imgFileName = os.path.basename(self.filePath)
        savedFileName = os.path.splitext(imgFileName)[0] + XML_EXT
        savedPath = os.path.join(imgFileDir, savedFileName)
        self._saveFile(savedPath)

    def saveFileAs(self, _value=False):
        assert not self.image.isNull(), "cannot save empty image"
        self._saveFile(self.saveFileDialog())

    def _saveFile(self, annotationFilePath):
        if annotationFilePath and self.saveLabels(annotationFilePath):
            self.setClean()
            self.statusBar().showMessage('Saved to  %s' % annotationFilePath)
            self.statusBar().show()

    def closeFile(self, _value=False):
        if not self.mayContinue():
            return

        self.resetState()
        self.setClean()
        self.toggleActions(False)
        self.canvas.setEnabled(False)

    def mayContinue(self):
        return not (self.dirty and not self.discardChangesDialog())

    def discardChangesDialog(self):
        yes, no = QMessageBox.Yes, QMessageBox.No
        msg = u'You have unsaved changes, proceed anyway?'

        return yes == QMessageBox.warning(self, u'Attention', msg, yes | no)

    def errorMessage(self, title, message):
        return QMessageBox.critical(self, title,
                                    '<p><b>%s</b></p>%s' % (title, message))

    def currentPath(self):
        return os.path.dirname(self.filePath) if self.filePath else '.'

    def chooseColor1(self):
        color = self.colorDialog.getColor(self.lineColor, u'Choose line color', default=DEFAULT_LINE_COLOR)
        if color:
            self.lineColor = color
            Shape.line_color = self.lineColor
            self.canvas.update()
            self.setDirty()

    def chooseColor2(self):
        color = self.colorDialog.getColor(self.fillColor, u'Choose fill color', default=DEFAULT_FILL_COLOR)

        if color:
            self.fillColor = color
            Shape.fill_color = self.fillColor
            self.canvas.update()
            self.setDirty()

    def deleteSelectedShape(self):
        self.remLabel(self.canvas.deleteSelected())
        self.setDirty()

    def chshapeLineColor(self):
        color = self.colorDialog.getColor(self.lineColor, u'Choose line color', default=DEFAULT_LINE_COLOR)

        if color:
            self.canvas.selectedShape.line_color = color
            self.canvas.update()
            self.setDirty()

    def chshapeFillColor(self):
        color = self.colorDialog.getColor(self.fillColor, u'Choose fill color', default=DEFAULT_FILL_COLOR)

        if color:
            self.canvas.selectedShape.fill_color = color
            self.canvas.update()
            self.setDirty()

    def copyShape(self):
        self.canvas.endMove(copy=True)
        self.addLabel(self.canvas.selectedShape)
        self.setDirty()

    def moveShape(self):
        self.canvas.endMove(copy=False)
        self.setDirty()

    def loadPredefinedClasses(self, predefClassesFile):
        if os.path.exists(predefClassesFile) is True:
            with codecs.open(predefClassesFile, 'r', 'utf8') as f:
                for line in f:
                    line = line.strip()

                    if self.labelHist is None:
                        self.lablHist = [line]
                    else:
                        self.labelHist.append(line)

    def loadPascalXMLByFilename(self, xmlPath):
        if self.filePath is None:
            return

        if os.path.isfile(xmlPath) is False:
            return

        tVocParseReader = PascalVocReader(xmlPath)
        shapes = tVocParseReader.getShapes()

        self.loadLabels(shapes)
        self.canvas.verified = tVocParseReader.verified


def inverted(color):
    return QColor(*[255 - v for v in color.getRgb()])


def read(filename, default=None):
    try:
        with open(filename, 'rb') as f:
            return f.read()
    except:
        return default


def get_main_app(argv=[]):
    app = QApplication(argv)
    app.setApplicationName(__appname__)
    app.setWindowIcon(newIcon("app"))
    win = MainWindow(argv[1] if len(argv) >= 2 else None,
                     argv[2] if len(argv) >= 3 else os.path.join(
                         os.path.dirname(sys.argv[0]),
                         'data', 'predefined_classes.txt'))
    win.show()

    return app, win


def main(argv=[]):
    app, _win = get_main_app(argv)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
