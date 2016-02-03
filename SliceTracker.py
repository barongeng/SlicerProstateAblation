import os
import csv
import numpy
import math, re, sys
import vtk, qt, ctk, slicer
from slicer.ScriptedLoadableModule import *
from SliceTrackerUtils.mixins import ModuleWidgetMixin, ModuleLogicMixin
from SliceTrackerUtils.helpers import SliceAnnotation, ExtendedQMessageBox
from Editor import EditorWidget
import EditorLib
import logging
from subprocess import Popen
from SliceTrackerUtils.ZFrameRegistration import LineMarkerRegistration


class DICOMTAGS:

  PATIENT_NAME          = '0010,0010'
  PATIENT_ID            = '0010,0020'
  PATIENT_BIRTH_DATE    = '0010,0030'
  SERIES_DESCRIPTION    = '0008,103E'
  SERIES_NUMBER         = '0020,0011'
  STUDY_DATE            = '0008,0020'
  STUDY_TIME            = '0008,0030'
  ACQUISITION_TIME      = '0008,0032'


class COLOR:

  RED = qt.QColor(qt.Qt.red)
  YELLOW = qt.QColor(qt.Qt.yellow)
  GREEN = qt.QColor(qt.Qt.darkGreen)
  GRAY = qt.QColor(qt.Qt.gray)


class STYLE:

  GRAY_BACKGROUND_WHITE_FONT  = 'background-color: rgb(130,130,130); ' \
                                           'color: rgb(255,255,255)'
  WHITE_BACKGROUND            = 'background-color: rgb(255,255,255)'
  LIGHT_GRAY_BACKGROUND       = 'background-color: rgb(230,230,230)'
  ORANGE_BACKGROUND           = 'background-color: rgb(255,102,0)'
  YELLOW_BACKGROUND           = 'background-color: yellow;'
  GREEN_BACKGROUND            = 'background-color: green;'
  GRAY_BACKGROUND             = 'background-color: gray;'
  RED_BACKGROUND              = 'background-color: red;'


class SliceTrackerConstants(object):

  LEFT_VIEWER_SLICE_ANNOTATION_TEXT = 'BIOPSY PLAN'
  RIGHT_VIEWER_SLICE_ANNOTATION_TEXT = 'TRACKED TARGETS'
  RIGHT_VIEWER_SLICE_TRANSFORMED_ANNOTATION_TEXT = 'OLD'
  RIGHT_VIEWER_SLICE_NEEDLE_IMAGE_ANNOTATION_TEXT = 'NEW'
  APPROVED_RESULT_TEXT_ANNOTATION = "approved"
  REJECTED_RESULT_TEXT_ANNOTATION = "rejected"
  SKIPPED_RESULT_TEXT_ANNOTATION = "skipped"

  LAYOUT_RED_SLICE_ONLY = slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView
  LAYOUT_FOUR_UP = slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView
  LAYOUT_SIDE_BY_SIDE = slicer.vtkMRMLLayoutNode.SlicerLayoutSideBySideView
  LAYOUT_THREE_OVER_THREE = slicer.vtkMRMLLayoutNode.SlicerLayoutThreeOverThreeView
  ALLOWED_LAYOUTS = [LAYOUT_SIDE_BY_SIDE, LAYOUT_FOUR_UP, LAYOUT_THREE_OVER_THREE]

  COVER_PROSTATE = "COVER PROSTATE"
  COVER_TEMPLATE = "COVER TEMPLATE"
  GUIDANCE_IMAGE = "GUIDANCE"


class SliceTracker(ScriptedLoadableModule):

  def __init__(self, parent):
    ScriptedLoadableModule.__init__(self, parent)
    self.parent.title = "SliceTracker"
    self.parent.categories = ["Radiology"]
    self.parent.dependencies = []
    self.parent.contributors = ["Peter Behringer (SPL), Christian Herz (SPL), Andriy Fedorov (SPL)"]
    self.parent.helpText = """ SliceTracker facilitates support of MRI-guided targeted prostate biopsy. """
    self.parent.acknowledgementText = """Surgical Planning Laboratory, Brigham and Women's Hospital, Harvard
                                          Medical School, Boston, USA This work was supported in part by the National
                                          Institutes of Health through grants U24 CA180918,
                                          R01 CA111288 and P41 EB015898."""


class SliceTrackerWidget(ScriptedLoadableModuleWidget, ModuleWidgetMixin, SliceTrackerConstants):

  DEFAULT_TEMPLATE_CONFIG_FILE_NAME = "Resources/zframe/ProstateTemplate.csv"

  @property
  def registrationResults(self):
    return self.logic.registrationResults

  @property
  def currentResult(self):
    return self.registrationResults.activeResult

  @currentResult.setter
  def currentResult(self, series):
    self.registrationResults.activeResult = series

  @property
  def preopDataDir(self):
    return self.logic.preopDataDir

  @preopDataDir.setter
  def preopDataDir(self, path):
    self.removeSliceAnnotations()
    self.hideAllMarkups()
    self.logic.preopDataDir = path
    self.setSetting('PreopLocation', path)
    self.loadPreopData()
    self.intraopSeriesSelector.clear()
    self.intraopDirButton.setEnabled(True)
    self.trackTargetsButton.setEnabled(False)
    self._updateOutputDir()
    self.preopDirButton.text = self.truncatePath(self.preopDirButton.directory)

  @property
  def intraopDataDir(self):
    return self.logic.intraopDataDir

  @intraopDataDir.setter
  def intraopDataDir(self, path):
    self.logic.zFrameRegistrationSuccessful = False
    self.skippedIntraopSeries = []
    self.collapsibleDirectoryConfigurationArea.collapsed = True
    self.logic.setReceivedNewImageDataCallback(self.onNewImageDataReceived)
    self.logic.intraopDataDir = path
    self.setSetting('IntraopLocation', path)
    self.intraopDirButton.text = self.truncatePath(self.intraopDirButton.directory)

  @property
  def outputDir(self):
    return self.logic.outputDir

  @outputDir.setter
  def outputDir(self, path):
    if os.path.exists(path):
      self._outputRoot = path
      self.setSetting('OutputLocation', path)
      self._updateOutputDir()
      self.caseCompletedButton.setEnabled(True)
      self.outputDirButton.text = self.truncatePath(self.outputDirButton.directory)

  def __init__(self, parent=None):
    ScriptedLoadableModuleWidget.__init__(self, parent)
    self.logic = SliceTrackerLogic()
    self.markupsLogic = slicer.modules.markups.logic()
    self.volumesLogic = slicer.modules.volumes.logic()
    self.modulePath = os.path.dirname(slicer.util.modulePath(self.moduleName))
    self.defaultTemplateFile = os.path.join(self.modulePath, self.DEFAULT_TEMPLATE_CONFIG_FILE_NAME)
    self.iconPath = os.path.join(self.modulePath, 'Resources/Icons')
    self._outputRoot = None
    self.setupIcons()

  def hideAllMarkups(self):
    fiducialNodes = slicer.mrmlScene.GetNodesByClass("vtkMRMLMarkupsFiducialNode")
    for itemNum in xrange(fiducialNodes.GetNumberOfItems()):
      node = fiducialNodes.GetItemAsObject(itemNum)
      self.markupsLogic.SetAllMarkupsVisibility(node, False)

  def onReload(self):
    ScriptedLoadableModuleWidget.onReload(self)
    try:
      self.logic = SliceTrackerLogic()
      self.removeSliceAnnotations()
      self.uncheckVisualEffects()
    except:
      pass

  def cleanup(self):
    ScriptedLoadableModuleWidget.cleanup(self)
    self.disconnectCrosshairNode()

  def _updateOutputDir(self):
    if self._outputRoot and self.patientID and self.currentStudyDate:
      time = qt.QTime().currentTime().toString().replace(":", "")
      dirName = self.patientID.text + "-biopsy-" + self.currentStudyDate.text + time
      self.logic.outputDir = os.path.join(self._outputRoot, dirName, "MRgBiopsy")

  def createPatientWatchBox(self):
    self.patientWatchBox, patientViewBoxLayout = self._createWatchBox(maximumHeight=90)

    self.patientID = qt.QLabel('None')
    self.patientName = qt.QLabel('None')
    self.patientBirthDate = qt.QLabel('None')
    self.preopStudyDate = qt.QLabel('None')
    self.currentStudyDate = qt.QLabel('None')

    patientViewBoxLayout.addWidget(self.createHLayout([qt.QLabel('Patient ID: '), self.patientID], margin=1))
    patientViewBoxLayout.addWidget(self.createHLayout([qt.QLabel('Patient Name: '), self.patientName], margin=1))
    patientViewBoxLayout.addWidget(self.createHLayout([qt.QLabel('Date of Birth: '), self.patientBirthDate], margin=1))
    patientViewBoxLayout.addWidget(self.createHLayout([qt.QLabel('Preop Study Date: '), self.preopStudyDate], margin=1))
    patientViewBoxLayout.addWidget(self.createHLayout([qt.QLabel('Current Study Date: '), self.currentStudyDate], margin=1))

  def createRegistrationWatchBox(self):
    self.registrationWatchBox, registrationWatchBoxLayout = self._createWatchBox(maximumHeight=40)
    self.currentRegisteredSeries = qt.QLabel('None')
    self.registrationSettingsButton = self.createButton("", icon=self.settingsIcon, styleSheet="border:none;",
                                                        maximumWidth=16)
    self.registrationSettingsButton.setCursor(qt.Qt.PointingHandCursor)
    registrationWatchBoxLayout.addWidget(self.createHLayout([qt.QLabel('Current Series:'), self.currentRegisteredSeries,
                                                             self.registrationSettingsButton], margin=1))
    self.registrationWatchBox.hide()

  def _createWatchBox(self, maximumHeight):
    watchBox = qt.QGroupBox()
    watchBox.maximumHeight = maximumHeight
    watchBox.setStyleSheet(STYLE.LIGHT_GRAY_BACKGROUND)
    watchBoxLayout = qt.QGridLayout()
    watchBox.setLayout(watchBoxLayout)
    self.layout.addWidget(watchBox)
    return watchBox, watchBoxLayout

  def setupIcons(self):
    self.cancelSegmentationIcon = self.createIcon('icon-cancelSegmentation.png')
    self.greenCheckIcon = self.createIcon('icon-greenCheck.png')
    self.quickSegmentationIcon = self.createIcon('icon-quickSegmentation.png')
    self.newImageDataIcon = self.createIcon('icon-newImageData.png')
    self.settingsIcon = self.createIcon('icon-settings.png')
    self.undoIcon = self.createIcon('icon-undo.png')
    self.redoIcon = self.createIcon('icon-redo.png')
    self.fourUpIcon = self.createIcon('icon-four-up.png')
    self.sideBySideIcon = self.createIcon('icon-side-by-side.png')
    self.threeOverThreeIcon = self.createIcon('icon-three-over-three.png')
    self.crosshairIcon = self.createIcon('icon-crosshair')
    self.zFrameIcon = self.createIcon('icon-zframe')
    self.needleIcon = self.createIcon('icon-needle')
    self.templateIcon = self.createIcon('icon-template')
    self.pathIcon = self.createIcon('icon-path')

  def setup(self):
    ScriptedLoadableModuleWidget.setup(self)

    try:
      import VolumeClipWithModel
    except ImportError:
      return self.warningDialog("Error: Could not find extension VolumeClip. Open Slicer Extension Manager and install "
                                "VolumeClip.", "Missing Extension")

    self.ratingWindow = RatingWindow(maximumValue=5)
    self.seriesItems = []
    self.sliceAnnotations = []
    self.revealCursor = None
    self.currentTargets = None
    self.evaluationModeOn = False

    self.crosshairNode = None
    self.crosshairNodeObserverTag = None

    self.logic.retryMode = False
    self.logic.zFrameRegistrationSuccessful = False

    self.notifyUserAboutNewData = True

    self.createPatientWatchBox()
    self.createRegistrationWatchBox()
    self.setupRegistrationStepUIElements()
    self.settingsArea()

    self.setupSliceWidgets()
    self.setupZFrameRegistrationUIElements()
    self.setupTargetingStepUIElements()
    self.setupSegmentationUIElements()
    self.setupEvaluationStepUIElements()

    self.setupConnections()

    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)
    self.setAxialOrientation()

    self.showAcceptRegistrationWarning = False

    self.logic.setupColorTable(colorFile=os.path.join(self.modulePath,'Resources/Colors/mpReviewColors.csv'))
    self.layout.addStretch()

  def settingsArea(self):
    self.collapsibleSettingsArea = ctk.ctkCollapsibleButton()
    self.collapsibleSettingsArea.text = "Settings"
    self.collapsibleSettingsArea.collapsed = True
    self.settingsAreaLayout = qt.QGridLayout(self.collapsibleSettingsArea)

    self.setupViewSettingGroupBox()
    self.setupZFrameViewSettingsGroupBox()

    self.settingsAreaLayout.addWidget(self.createHLayout([self.viewSettingsGroupBox, self.zFrameViewSettingsGroupBox]))
    self.layout.addWidget(self.collapsibleSettingsArea)

  def setupViewSettingGroupBox(self):
    self.setupLayoutsButton()
    self.setupCrosshairButton()
    self.viewSettingsGroupBox = qt.QGroupBox('View options:')
    viewSettingsLayout = qt.QVBoxLayout()
    self.viewSettingsGroupBox.setLayout(viewSettingsLayout)
    viewSettingsLayout.addWidget(self.createVLayout([self.layoutsMenuButton, self.crosshairButton]))

  def setupLayoutsButton(self):
    self.layoutsMenuButton = self.createButton("Layouts")
    self.layoutsMenu = qt.QMenu()
    self.layoutDict = dict()
    self.layoutDict[self.LAYOUT_SIDE_BY_SIDE] = self.layoutsMenu.addAction(self.sideBySideIcon, "side-by-side")
    self.layoutDict[self.LAYOUT_FOUR_UP] = self.layoutsMenu.addAction(self.fourUpIcon, "Four-Up")
    self.layoutDict[self.LAYOUT_THREE_OVER_THREE] = self.layoutsMenu.addAction(self.threeOverThreeIcon, "Three over three")
    self.layoutsMenuButton.setMenu(self.layoutsMenu)

  def setupCrosshairButton(self):
    self.crosshairButton = self.createButton("Show crosshair", checkable=True, icon=self.crosshairIcon)
    self.crosshairNode = slicer.mrmlScene.GetNthNodeByClass(0, 'vtkMRMLCrosshairNode')

  def setupZFrameViewSettingsGroupBox(self):
    self.zFrameViewSettingsGroupBox = qt.QGroupBox('Z-Frame options:')
    viewSettingsLayout = qt.QVBoxLayout()
    self.zFrameViewSettingsGroupBox.setLayout(viewSettingsLayout)
    self.showZFrameModelCheckbox = self.createButton("", icon=self.zFrameIcon, checkable=True, toolTip="Display zframe model")
    self.showZFrameTemplateCheckbox = self.createButton("", icon=self.templateIcon, checkable=True, toolTip="Display template")
    self.showNeedlePathCheckbox = self.createButton("", icon=self.needleIcon, checkable=True, toolTip="Display needle path")
    self.showTemplatePathCheckbox = self.createButton("", icon=self.pathIcon, checkable=True, toolTip="Display template paths")

    self.showZFrameTemplateCheckbox.enabled = self.logic.loadTemplateConfigFile(self.defaultTemplateFile)
    viewSettingsLayout.addWidget(self.createHLayout([self.showZFrameModelCheckbox, self.showZFrameTemplateCheckbox]))
    viewSettingsLayout.addWidget(self.createHLayout([ self.showTemplatePathCheckbox, self.showNeedlePathCheckbox]))

  def setupSliceWidgets(self):
    self.setupSliceWidget("Red")
    self.setupSliceWidget("Yellow")
    self.setupSliceWidget("Green")
    self.layoutManager.setLayout(self.LAYOUT_THREE_OVER_THREE)
    self.setupSliceWidget("Slice4")
    self.setupSliceWidget("Slice5")
    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)

  def setupSliceWidget(self, name):
    widget = self.layoutManager.sliceWidget(name)
    setattr(self, name.lower()+"Widget", widget)
    setattr(self, name.lower()+"CompositeNode", widget.mrmlSliceCompositeNode())
    setattr(self, name.lower()+"SliceView", widget.sliceView())
    logic = widget.sliceLogic()
    setattr(self, name.lower()+"SliceLogic", logic)
    setattr(self, name.lower()+"SliceNode", logic.GetSliceNode())
    setattr(self, name.lower()+"FOV", [])

  def setDefaultOrientation(self):
    self.redSliceNode.SetOrientationToAxial()
    self.yellowSliceNode.SetOrientationToSagittal()
    self.greenSliceNode.SetOrientationToCoronal()

  def setAxialOrientation(self):
    self.redSliceNode.SetOrientationToAxial()
    self.yellowSliceNode.SetOrientationToAxial()
    self.greenSliceNode.SetOrientationToAxial()

  def setSagittalOrientation(self):
    self.slice4SliceNode.SetOrientationToSagittal()
    self.slice5SliceNode.SetOrientationToSagittal()

  def setupZFrameRegistrationUIElements(self):
    self.zFrameRegistrationGroupBox = qt.QGroupBox()
    self.zFrameRegistrationGroupBoxGroupBoxLayout = qt.QGridLayout()
    self.zFrameRegistrationGroupBox.setLayout(self.zFrameRegistrationGroupBoxGroupBoxLayout)
    self.zFrameRegistrationGroupBox.hide()

    self.approveZFrameRegistrationButton = self.createButton("Confirm registration accuracy")

    self.zFrameRegistrationGroupBoxGroupBoxLayout.addWidget(self.approveZFrameRegistrationButton)
    self.layout.addWidget(self.zFrameRegistrationGroupBox)

  def setupTargetingStepUIElements(self):
    self.targetingGroupBox = qt.QGroupBox()
    self.targetingGroupBoxLayout = qt.QGridLayout()
    self.targetingGroupBox.setLayout(self.targetingGroupBoxLayout)

    self.preopDirButton = self.createDirectoryButton(text="Preop Directory", caption="Choose Preop Location",
                                                     directory=self.getSetting('PreopLocation'))
    self.outputDirButton = self.createDirectoryButton(caption="Choose Data Output Location")
    self.intraopDirButton = self.createDirectoryButton(text="Intraop Directory", caption="Choose Intraop Location",
                                                       directory=self.getSetting('IntraopLocation'), enabled=False)

    self.trackTargetsButton = self.createButton("Track targets", toolTip="Track targets", enabled=False)
    self.skipIntraopSeriesButton = self.createButton("Skip", toolTip="Skip the currently selected series", enabled=False)
    self.caseCompletedButton = self.createButton('Case completed', enabled=os.path.exists(self.getSetting('OutputLocation')))
    self.setupTargetsTable()
    self.setupIntraopSeriesSelector()
    self.outputDirButton.directory = self.truncatePath(self.getSetting('OutputLocation'))
    self._outputRoot = self.outputDirButton.directory
    self.caseCompletedButton.setEnabled(self._outputRoot is not None)

    self.collapsibleDirectoryConfigurationArea = ctk.ctkCollapsibleButton()
    self.collapsibleDirectoryConfigurationArea.text = "Directory Settings"
    self.directoryConfigurationLayout = qt.QGridLayout(self.collapsibleDirectoryConfigurationArea)
    self.directoryConfigurationLayout.addWidget(self.preopDirButton, 1, 0, 1, qt.QSizePolicy.ExpandFlag)
    self.directoryConfigurationLayout.addWidget(self.createHelperLabel("Please select the mpReview preprocessed preop "
                                                                       "data here."), 1, 1, 1, 1, qt.Qt.AlignRight)
    self.directoryConfigurationLayout.addWidget(self.outputDirButton, 2, 0, 1, qt.QSizePolicy.ExpandFlag)
    self.directoryConfigurationLayout.addWidget(self.createHelperLabel("Please select the output directory where all "
                                                                       "data will be saved"), 2, 1, 1, 1, qt.Qt.AlignRight)
    self.directoryConfigurationLayout.addWidget(self.intraopDirButton, 3, 0, 1, qt.QSizePolicy.ExpandFlag)
    self.directoryConfigurationLayout.addWidget(self.createHelperLabel("Please select the intraop directory where new "
                                                                       "DICOM data will be arriving during the biopsy")
                                                , 3, 1, 1, 1, qt.Qt.AlignRight)

    self.targetingGroupBoxLayout.addWidget(self.collapsibleDirectoryConfigurationArea, 0, 0, 1, 2)
    self.targetingGroupBoxLayout.addWidget(self.targetTable, 1, 0, 1, 2)
    self.targetingGroupBoxLayout.addWidget(self.intraopSeriesSelector, 2, 0)
    self.targetingGroupBoxLayout.addWidget(self.skipIntraopSeriesButton, 2, 1)
    self.targetingGroupBoxLayout.addWidget(self.trackTargetsButton, 3, 0, 1, 2)
    self.targetingGroupBoxLayout.addWidget(self.caseCompletedButton, 4, 0, 1, 2)
    self.layout.addWidget(self.targetingGroupBox)

  def createHelperLabel(self, toolTipText=""):
    helperPixmap = qt.QPixmap(os.path.join(self.iconPath, 'icon-infoBox.png'))
    helperPixmap = helperPixmap.scaled(qt.QSize(20, 20))
    label = self.createLabel("", pixmap=helperPixmap, toolTip=toolTipText)
    label.setCursor(qt.Qt.PointingHandCursor)
    return label

  def setupTargetsTable(self):
    self.targetTable = qt.QTableView()
    self.model = CustomTargetTableModel(self.logic)
    self.targetTable.setModel(self.model)
    self.targetTable.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
    self.targetTable.horizontalHeader().setResizeMode(qt.QHeaderView.Stretch)
    self.targetTable.verticalHeader().hide()
    self.targetTable.maximumHeight = 150

  def setupIntraopSeriesSelector(self):
    self.intraopSeriesSelector = qt.QComboBox()
    self.seriesModel = qt.QStandardItemModel()
    self.intraopSeriesSelector.setModel(self.seriesModel)

  def setupSegmentationUIElements(self):
    iconSize = qt.QSize(24, 24)

    self.referenceVolumeSelector = self.createComboBox(nodeTypes=["vtkMRMLScalarVolumeNode", ""], noneEnabled=True,
                                                       selectNodeUponCreation=True, showChildNodeTypes=False)
    self.quickSegmentationButton = self.createButton('Quick Mode', icon=self.quickSegmentationIcon, iconSize=iconSize,
                                                     styleSheet=STYLE.WHITE_BACKGROUND)
    self.applySegmentationButton = self.createButton("", icon=self.greenCheckIcon, iconSize=iconSize,
                                                     styleSheet=STYLE.WHITE_BACKGROUND, enabled=False)
    self.cancelSegmentationButton = self.createButton("", icon=self.cancelSegmentationIcon,
                                                      iconSize=iconSize, enabled=False)
    self.backButton = self.createButton("", icon=self.undoIcon, iconSize=iconSize, enabled=False)
    self.forwardButton = self.createButton("", icon=self.redoIcon, iconSize=iconSize, enabled=False)

    self.applyRegistrationButton = self.createButton("Apply Registration", icon=self.greenCheckIcon, iconSize=iconSize,
                                                     toolTip="Run Registration.")
    self.applyRegistrationButton.setFixedHeight(45)

    self.editorWidgetButton = self.createButton("", icon=self.settingsIcon, toolTip="Show Label Editor",
                                                enabled=False, iconSize=iconSize)

    segmentationButtons = self.createHLayout([self.quickSegmentationButton, self.applySegmentationButton,
                                              self.cancelSegmentationButton, self.backButton, self.forwardButton,
                                              self.editorWidgetButton])
    self.setupEditorWidget()

    self.segmentationGroupBox = qt.QGroupBox()
    self.segmentationGroupBoxLayout = qt.QFormLayout()
    self.segmentationGroupBox.setLayout(self.segmentationGroupBoxLayout)
    self.segmentationGroupBoxLayout.addWidget(segmentationButtons)
    self.segmentationGroupBoxLayout.addRow(self.editorWidgetParent)
    self.segmentationGroupBoxLayout.addRow(self.applyRegistrationButton)
    self.segmentationGroupBox.hide()
    self.editorWidgetParent.hide()

  def setupEditorWidget(self):
    self.editorWidgetParent = slicer.qMRMLWidget()
    self.editorWidgetParent.setLayout(qt.QVBoxLayout())
    self.editorWidgetParent.setMRMLScene(slicer.mrmlScene)
    self.editUtil = EditorLib.EditUtil.EditUtil()
    self.editorWidget = EditorWidget(parent=self.editorWidgetParent, showVolumesFrame=False)
    self.editorWidget.setup()
    self.editorParameterNode = self.editUtil.getParameterNode()

  def setupRegistrationStepUIElements(self):
    self.registrationGroupBox = qt.QGroupBox()
    self.registrationGroupBoxLayout = qt.QFormLayout()
    self.registrationGroupBox.setLayout(self.registrationGroupBoxLayout)
    self.preopVolumeSelector = self.createComboBox(nodeTypes=["vtkMRMLScalarVolumeNode", ""], showChildNodeTypes=False,
                                                   selectNodeUponCreation=True, toolTip="Pick algorithm input.")
    self.preopLabelSelector = self.createComboBox(nodeTypes=["vtkMRMLLabelMapVolumeNode", ""], showChildNodeTypes=False,
                                                  selectNodeUponCreation=False, toolTip="Pick algorithm input.")
    self.intraopVolumeSelector = self.createComboBox(nodeTypes=["vtkMRMLScalarVolumeNode", ""], noneEnabled=True,
                                                     showChildNodeTypes=False, selectNodeUponCreation=True,
                                                     toolTip="Pick algorithm input.")
    self.intraopLabelSelector = self.createComboBox(nodeTypes=["vtkMRMLLabelMapVolumeNode", ""],
                                                    showChildNodeTypes=False,
                                                    selectNodeUponCreation=True, toolTip="Pick algorithm input.")
    self.fiducialSelector = self.createComboBox(nodeTypes=["vtkMRMLMarkupsFiducialNode", ""], noneEnabled=True,
                                                showChildNodeTypes=False, selectNodeUponCreation=False,
                                                toolTip="Select the Targets")
    self.registrationGroupBoxLayout.addRow("Preop Image Volume: ", self.preopVolumeSelector)
    self.registrationGroupBoxLayout.addRow("Preop Label Volume: ", self.preopLabelSelector)
    self.registrationGroupBoxLayout.addRow("Intraop Image Volume: ", self.intraopVolumeSelector)
    self.registrationGroupBoxLayout.addRow("Intraop Label Volume: ", self.intraopLabelSelector)
    self.registrationGroupBoxLayout.addRow("Targets: ", self.fiducialSelector)
    self.registrationGroupBox.hide()
    self.layout.addWidget(self.registrationGroupBox)

  def setupEvaluationStepUIElements(self):
    self.registrationEvaluationGroupBox = qt.QGroupBox()
    self.registrationEvaluationGroupBoxLayout = qt.QGridLayout()
    self.registrationEvaluationGroupBox.setLayout(self.registrationEvaluationGroupBoxLayout)
    self.registrationEvaluationGroupBox.hide()

    self.setupCollapsibleRegistrationArea()
    self.setupRegistrationValidationButtons()
    self.registrationEvaluationGroupBoxLayout.addWidget(self.segmentationGroupBox, 2, 0)
    self.registrationEvaluationGroupBoxLayout.addWidget(self.collapsibleRegistrationArea, 3, 0)
    self.registrationEvaluationGroupBoxLayout.addWidget(self.evaluationButtonsGroupBox, 5, 0)
    self.layout.addWidget(self.registrationEvaluationGroupBox)

  def setupRegistrationValidationButtons(self):
    self.approveRegistrationResultButton = self.createButton("Approve")
    self.retryRegistrationButton = self.createButton("Retry")
    self.skipRegistrationResultButton = self.createButton("Skip")
    self.rejectRegistrationResultButton = self.createButton("Reject")
    self.evaluationButtonsGroupBox = self.createHLayout([self.skipRegistrationResultButton, self.retryRegistrationButton,
                                                         self.approveRegistrationResultButton, self.rejectRegistrationResultButton])
    self.evaluationButtonsGroupBox.enabled = False

  def setupCollapsibleRegistrationArea(self):
    self.collapsibleRegistrationArea = ctk.ctkCollapsibleButton()
    self.collapsibleRegistrationArea.text = "Registration Results"
    self.registrationGroupBoxDisplayLayout = qt.QFormLayout(self.collapsibleRegistrationArea)

    #TODO: selector should be used to show only registrations (retried) of the current series
    self.resultSelector = ctk.ctkComboBox()
    self.resultSelector.setFixedWidth(250)
    self.registrationResultAlternatives = self.createHLayout([qt.QLabel('Alternative Registration Result'), self.resultSelector])
    self.registrationGroupBoxDisplayLayout.addWidget(self.registrationResultAlternatives)

    self.showRigidResultButton = self.createButton('Rigid')
    self.showAffineResultButton = self.createButton('Affine')
    self.showBSplineResultButton = self.createButton('BSpline')

    self.registrationButtonGroup = qt.QButtonGroup()
    self.registrationButtonGroup.addButton(self.showRigidResultButton, 1)
    self.registrationButtonGroup.addButton(self.showAffineResultButton, 2)
    self.registrationButtonGroup.addButton(self.showBSplineResultButton, 3)

    self.registrationGroupBoxDisplayLayout.addWidget(
      self.createHLayout([self.showRigidResultButton, self.showAffineResultButton, self.showBSplineResultButton]))

    self.setupVisualEffectsUIElements()
    self.registrationGroupBoxDisplayLayout.addWidget(self.visualEffectsGroupBox)

  def setupVisualEffectsUIElements(self):
    self.fadeSlider = ctk.ctkSliderWidget()
    self.fadeSlider.minimum = 0
    self.fadeSlider.maximum = 1.0
    self.fadeSlider.value = 0
    self.fadeSlider.singleStep = 0.05

    self.rockCount = 0
    self.rockTimer = qt.QTimer()
    self.rockTimer.setInterval(50)
    self.rockCheckBox = qt.QCheckBox("Rock")
    self.rockCheckBox.checked = False

    self.flickerTimer = qt.QTimer()
    self.flickerTimer.setInterval(400)
    self.flickerCheckBox = qt.QCheckBox("Flicker")
    self.flickerCheckBox.checked = False

    self.animaHolderLayout = self.createVLayout([self.rockCheckBox, self.flickerCheckBox])
    self.revealCursorCheckBox = qt.QCheckBox("Use RevealCursor")
    self.revealCursorCheckBox.checked = False

    self.visualEffectsGroupBox = qt.QGroupBox("Visual Evaluation")
    self.visualEffectsGroupBoxLayout = qt.QFormLayout(self.visualEffectsGroupBox)
    self.visualEffectsGroupBoxLayout.addWidget(self.createHLayout([qt.QLabel('Opacity'), self.fadeSlider,
                                                                   self.animaHolderLayout]))
    self.visualEffectsGroupBoxLayout.addRow("", self.revealCursorCheckBox)

  def setupConnections(self):

    def setupButtonConnections():
      self.preopDirButton.directorySelected.connect(lambda: setattr(self, "preopDataDir", self.preopDirButton.directory))
      self.outputDirButton.directorySelected.connect(lambda: setattr(self, "outputDir", self.outputDirButton.directory))
      self.intraopDirButton.directorySelected.connect(lambda: setattr(self, "intraopDataDir", self.intraopDirButton.directory))
      self.forwardButton.clicked.connect(self.onForwardButtonClicked)
      self.backButton.clicked.connect(self.onBackButtonClicked)
      self.editorWidgetButton.clicked.connect(self.onEditorGearIconClicked)
      self.applyRegistrationButton.clicked.connect(lambda: self.onInvokeRegistration(initial=True))
      self.quickSegmentationButton.clicked.connect(self.onQuickSegmentationButtonClicked)
      self.cancelSegmentationButton.clicked.connect(self.onCancelSegmentationButtonClicked)
      self.trackTargetsButton.clicked.connect(self.onTrackTargetsButtonClicked)
      self.applySegmentationButton.clicked.connect(self.onApplySegmentationButtonClicked)
      self.approveRegistrationResultButton.clicked.connect(self.onApproveRegistrationResultButtonClicked)
      self.skipRegistrationResultButton.clicked.connect(self.onSkipRegistrationResultButtonClicked)
      self.skipIntraopSeriesButton.clicked.connect(self.onSkipIntraopSeriesButtonClicked)
      self.rejectRegistrationResultButton.clicked.connect(self.onRejectRegistrationResultButtonClicked)
      self.retryRegistrationButton.clicked.connect(self.onRetryRegistrationButtonClicked)
      self.caseCompletedButton.clicked.connect(self.onSaveDataButtonClicked)
      self.registrationSettingsButton.clicked.connect(self.showRegistrationDetails)
      self.registrationButtonGroup.connect('buttonClicked(int)', self.onRegistrationButtonChecked)
      self.crosshairButton.clicked.connect(self.onCrosshairButtonClicked)
      self.approveZFrameRegistrationButton.clicked.connect(self.onApproveZFrameRegistrationButtonClicked)

    def setupSelectorConnections():
      self.resultSelector.connect('currentIndexChanged(QString)', self.onRegistrationResultSelected)
      self.intraopSeriesSelector.connect('currentIndexChanged(QString)', self.onIntraopSeriesSelectionChanged)

    def setupCheckBoxConnections():
      self.rockCheckBox.connect('toggled(bool)', self.onRockToggled)
      self.flickerCheckBox.connect('toggled(bool)', self.onFlickerToggled)
      self.revealCursorCheckBox.connect('toggled(bool)', self.revealToggled)
      self.showZFrameModelCheckbox.connect('toggled(bool)', self.onShowZFrameModelToggled)
      self.showZFrameTemplateCheckbox.connect('toggled(bool)', self.onShowZFrameTemplateToggled)
      self.showTemplatePathCheckbox.connect('toggled(bool)', self.onShowTemplatePathToggled)
      self.showNeedlePathCheckbox.connect('toggled(bool)', self.onShowNeedlePathToggled)

    def setupOtherConnections():
      self.fadeSlider.connect('valueChanged(double)', self.changeOpacity)
      self.rockTimer.connect('timeout()', self.onRockToggled)
      self.flickerTimer.connect('timeout()', self.onFlickerToggled)
      self.targetTable.connect('clicked(QModelIndex)', self.jumpToTarget)
      self.layoutsMenu.triggered.connect(self.onLayoutSelectionChanged)
      self.layoutManager.layoutChanged.connect(self.layoutChanged)

    setupCheckBoxConnections()
    setupButtonConnections()
    setupSelectorConnections()
    setupOtherConnections()

  def onShowZFrameModelToggled(self, checked):
    self.logic.zFrameModelNode.SetDisplayVisibility(checked)

  def onShowZFrameTemplateToggled(self, checked):
    self.logic.setTemplateVisibility(checked)

  def onShowTemplatePathToggled(self, checked):
    self.logic.setTemplatePathVisibility(checked)

  def onShowNeedlePathToggled(self, checked):
    self.logic.setNeedlePathVisibility(checked)

  def showRegistrationDetails(self):
    if self.registrationGroupBox.visible:
      self.registrationGroupBox.hide()
      self.registrationGroupBox.enabled = True
    else:
      self.registrationGroupBox.show()
      self.registrationGroupBox.enabled = False

  def layoutChanged(self):
    if self.layoutManager.layout in self.ALLOWED_LAYOUTS:
      self.layoutsMenu.setActiveAction(self.layoutDict[self.layoutManager.layout])
      self.onLayoutSelectionChanged(self.layoutDict[self.layoutManager.layout])
    else:
      self.layoutsMenuButton.setIcon(qt.QIcon())
      self.layoutsMenuButton.setText("Layouts")

  def onLayoutSelectionChanged(self, action):
    self.layoutsMenuButton.setIcon(action.icon)
    self.layoutsMenuButton.setText(action.text)
    selectedLayout = self.getLayoutByAction(action)
    if self.layoutManager.layout != selectedLayout:
      self.layoutManager.setLayout(selectedLayout)

  def getLayoutByAction(self, searchedAction):
    for layout, action in self.layoutDict.iteritems():
      if action is searchedAction:
        return layout

  def onCrosshairButtonClicked(self):
    if self.crosshairButton.checked:
      self.crosshairNode.SetCrosshairMode(self.crosshairNode.ShowSmallBasic)
      self.crosshairButton.setText("Hide crosshair")
    else:
      self.crosshairButton.setText("Show crosshair")
      self.crosshairNode.SetCrosshairMode(0)

  def onRegistrationButtonChecked(self, buttonId):
    self.hideAllTargets()
    if buttonId == 1:
      self.onRigidResultClicked()
    elif buttonId == 2:
      if not self.currentResult.affineTargets:
        return self.onRegistrationButtonChecked(3)
      self.onAffineResultClicked()
    elif buttonId == 3:
      self.onBSplineResultClicked()
    self.activeRegistrationResultButtonId = buttonId

  def deactivateUndoRedoButtons(self):
    self.forwardButton.setEnabled(0)
    self.backButton.setEnabled(0)

  def updateUndoRedoButtons(self, observer=None, caller=None):
    self.forwardButton.setEnabled(self.deletedMarkups.GetNumberOfFiducials() > 0)
    self.backButton.setEnabled(self.logic.inputMarkupNode.GetNumberOfFiducials() > 0)

  def onIntraopSeriesSelectionChanged(self, selectedSeries=None):
    self.removeSliceAnnotations()
    if selectedSeries:
      trackingPossible = self.isTrackingPossible(selectedSeries)
      self.trackTargetsButton.setEnabled(trackingPossible)
      self.skipIntraopSeriesButton.setEnabled(trackingPossible)
      self.configureColorsAndViewersForSelectedIntraopSeries(selectedSeries)
      self.setIntraopSeriesSelectorColorAndSliceAnnotations(selectedSeries)

  def isTrackingPossible(self, series):
    return self.logic.isTrackingPossible(series) and not self.wasSeriesSkipped(series) and \
          (self.registrationResults.getMostRecentApprovedCoverProstateRegistration() or
          (self.COVER_PROSTATE in series and self.logic.zFrameRegistrationSuccessful) or
          (self.COVER_TEMPLATE in series and not self.logic.zFrameRegistrationSuccessful))

  def setIntraopSeriesSelectorColorAndSliceAnnotations(self, selectedSeries):
    style = STYLE.YELLOW_BACKGROUND
    if not self.isTrackingPossible(selectedSeries):
      annotationText = None
      if self.registrationResults.registrationResultWasApproved(selectedSeries):
        style = STYLE.GREEN_BACKGROUND
        annotationText = self.APPROVED_RESULT_TEXT_ANNOTATION
      elif self.registrationResults.registrationResultWasSkipped(selectedSeries) or self.wasSeriesSkipped(selectedSeries):
        style = STYLE.RED_BACKGROUND
        annotationText = self.SKIPPED_RESULT_TEXT_ANNOTATION
      elif self.registrationResults.registrationResultWasRejected(selectedSeries):
        style = STYLE.GRAY_BACKGROUND
        annotationText = self.REJECTED_RESULT_TEXT_ANNOTATION
      if annotationText:
        self.sliceAnnotations.append(SliceAnnotation(self.yellowWidget, annotationText, fontSize=15, yPos=20))
    self.intraopSeriesSelector.setStyleSheet(style)

  def configureColorsAndViewersForSelectedIntraopSeries(self, selectedSeries):
    if self.wasSeriesSkipped(selectedSeries):
      return
    if self.registrationResults.registrationResultWasApproved(selectedSeries):
      self.showRegistrationResultSideBySideForSelectedSeries()
    elif self.registrationResults.registrationResultWasSkipped(selectedSeries):
      self.showRegistrationResultSideBySideForSelectedSeries()
    else:
      try:
        result = self.registrationResults.getResultsBySeries(selectedSeries)[0]
      except IndexError:
        self.setupScreenForDisplayingSeries(None)
        return
      self.setupScreenForDisplayingSeries(result.fixedVolume.GetID())

  def uncheckVisualEffects(self):
    self.flickerCheckBox.checked = False
    self.rockCheckBox.checked = False
    self.revealCursorCheckBox.checked = False

  def setupScreenForDisplayingSeries(self, volumeID):
    self.disableTargetTable()
    self.layoutManager.setLayout(self.LAYOUT_FOUR_UP)
    self.setBackgroundToVolume(volumeID)
    self.setDefaultOrientation()
    slicer.app.applicationLogic().FitSliceToAll()

  def showRegistrationResultSideBySideForSelectedSeries(self):
    self.targetTable.enabled = True
    for result in self.registrationResults.getResultsBySeries(self.intraopSeriesSelector.currentText):
      if result.approved or result.skipped:
        self.setupRegistrationResultView()
        self.onRegistrationResultSelected(result.name)
        break

  def jumpToTarget(self, modelIndex=None):
    if not modelIndex:
      try:
        modelIndex = self.targetTable.selectedIndexes()[0]
      except IndexError:
        self.targetTable.selectRow(0)
        modelIndex = self.targetTable.selectedIndexes()[0]
    row = modelIndex.row()
    if not self.currentTargets:
      self.currentTargets = self.preopTargets
    self.markupsLogic.SetAllMarkupsVisibility(self.currentTargets, True)
    self.jumpSliceNodeToTarget(self.redSliceNode, self.preopTargets, row)
    self.jumpSliceNodeToTarget(self.slice4SliceNode, self.preopTargets, row)
    self.setTargetSelected(self.preopTargets, selected=False)
    self.preopTargets.SetNthFiducialSelected(row, True)
    self.jumpSliceNodeToTarget(self.yellowSliceNode, self.currentTargets, row)
    self.jumpSliceNodeToTarget(self.slice5SliceNode, self.currentTargets, row)
    self.setTargetSelected(self.currentTargets, selected=False)
    self.currentTargets.SetNthFiducialSelected(row, True)

  def jumpSliceNodeToTarget(self, sliceNode, targetNode, n):
    point = [0,0,0,0]
    targetNode.GetMarkupPointWorld(n, 0, point)
    sliceNode.JumpSlice(point[0], point[1], point[2])

  def updateRegistrationResultSelector(self):
    self.resultSelector.clear()
    results = self.registrationResults.getResultsBySeriesNumber(self.currentResult.seriesNumber)
    for result in reversed(results):
      self.resultSelector.addItem(result.name)
    self.registrationResultAlternatives.visible = len(results) > 1

  def removeSliceAnnotations(self):
    for annotation in self.sliceAnnotations:
      annotation.remove()
    self.sliceAnnotations = []

  def addSliceAnnotations(self):
    self.removeSliceAnnotations()
    self.sliceAnnotations.append(SliceAnnotation(self.redWidget, self.LEFT_VIEWER_SLICE_ANNOTATION_TEXT, fontSize=30,
                                                 yPos=55))
    self.sliceAnnotations.append(SliceAnnotation(self.slice4Widget, self.LEFT_VIEWER_SLICE_ANNOTATION_TEXT, fontSize=30,
                                                 yPos=55))
    self.sliceAnnotations.append(SliceAnnotation(self.yellowWidget, self.RIGHT_VIEWER_SLICE_ANNOTATION_TEXT, yPos=55,
                                                 fontSize=30))
    self.sliceAnnotations.append(SliceAnnotation(self.slice5Widget, self.RIGHT_VIEWER_SLICE_ANNOTATION_TEXT, yPos=55,
                                                 fontSize=30))
    self.rightViewerNewImageAnnotation = SliceAnnotation(self.yellowWidget,
                                                         self.RIGHT_VIEWER_SLICE_NEEDLE_IMAGE_ANNOTATION_TEXT, yPos=35,
                                                         opacity=0.0, color=(0,0.5,0))
    self.sliceAnnotations.append(self.rightViewerNewImageAnnotation)
    self.rightViewerOldImageAnnotation = SliceAnnotation(self.yellowWidget,
                                                         self.RIGHT_VIEWER_SLICE_TRANSFORMED_ANNOTATION_TEXT, yPos=35)
    self.sliceAnnotations.append(self.rightViewerOldImageAnnotation)
    self.rightViewerRegistrationResultStatusAnnotation = None

  def onForwardButtonClicked(self):
    numberOfDeletedTargets = self.deletedMarkups.GetNumberOfFiducials()
    logging.debug(('numberOfTargets in deletedMarkups is' + str(numberOfDeletedTargets)))
    pos = [0.0, 0.0, 0.0]

    if numberOfDeletedTargets > 0:
      self.deletedMarkups.GetNthFiducialPosition(numberOfDeletedTargets - 1, pos)

    logging.debug(('deletedMarkups.position = ' + str(pos)))

    if pos == [0.0, 0.0, 0.0]:
      logging.debug('pos was 0,0,0 -> go on')
    else:
      self.logic.inputMarkupNode.AddFiducialFromArray(pos)

      # delete it in deletedMarkups
      self.deletedMarkups.RemoveMarkup(numberOfDeletedTargets - 1)

    self.updateUndoRedoButtons()

  def onBackButtonClicked(self):
    activeFiducials = self.logic.inputMarkupNode
    numberOfTargets = activeFiducials.GetNumberOfFiducials()
    logging.debug('numberOfTargets is' + str(numberOfTargets))
    pos = [0.0, 0.0, 0.0]
    activeFiducials.GetNthFiducialPosition(numberOfTargets - 1, pos)
    logging.debug('activeFiducials.position = ' + str(pos))

    if numberOfTargets > 0:
      self.deletedMarkups.GetNthFiducialPosition(numberOfTargets - 1, pos)

    activeFiducials.GetNthFiducialPosition(numberOfTargets - 1, pos)
    logging.debug('POS BEFORE ENTRY = ' + str(pos))
    if pos == [0.0, 0.0, 0.0]:
      logging.debug('pos was 0,0,0 -> go on')
    else:
      # add it to deletedMarkups
      activeFiducials.GetNthFiducialPosition(numberOfTargets - 1, pos)
      # logging.debug(('pos = '+str(pos))
      self.deletedMarkups.AddFiducialFromArray(pos)
      logging.debug('added Markup with position ' + str(pos) + ' to the deletedMarkupsList')
      # delete it in activeFiducials
      activeFiducials.RemoveMarkup(numberOfTargets - 1)

    self.updateUndoRedoButtons()

  def revealToggled(self, checked):
    if self.revealCursor:
      self.revealCursor.tearDown()
    if checked:
      import CompareVolumes
      self.revealCursor = CompareVolumes.LayerReveal()

  def setOldNewIndicatorAnnotationOpacity(self, value):
    self.rightViewerNewImageAnnotation.opacity = value
    self.rightViewerOldImageAnnotation.opacity = 1.0-value

  def onRockToggled(self):

    def startRocking():
      self.flickerCheckBox.setEnabled(False)
      self.rockTimer.start()
      self.fadeSlider.value = 0.5 + math.sin(self.rockCount / 10.) / 2.
      self.rockCount += 1

    def stopRocking():
      self.flickerCheckBox.setEnabled(True)
      self.rockTimer.stop()
      self.fadeSlider.value = 0.0

    if self.rockCheckBox.checked:
      startRocking()
    else:
      stopRocking()

  def onFlickerToggled(self):

    def startFlickering():
      self.rockCheckBox.setEnabled(False)
      self.flickerTimer.start()
      self.fadeSlider.value = 1.0 if self.fadeSlider.value == 0.0 else 0.0

    def stopFlickering():
      self.rockCheckBox.setEnabled(True)
      self.flickerTimer.stop()
      self.fadeSlider.value = 0.0

    if self.flickerCheckBox.checked:
      startFlickering()
    else:
      stopFlickering()

  def onSaveDataButtonClicked(self):
    self.save(showDialog=True)

  def save(self, showDialog=False):
    message = self.logic.save()
    if showDialog:
      self.notificationDialog(message)

  def configureSegmentationMode(self):
    self.referenceVolumeSelector.setCurrentNode(self.logic.currentIntraopVolume)
    self.intraopVolumeSelector.setCurrentNode(self.logic.currentIntraopVolume)
    self.applyRegistrationButton.setEnabled(False)
    self.quickSegmentationButton.setEnabled(self.referenceVolumeSelector.currentNode() is not None)
    self.setupScreenForDisplayingSeries(self.logic.currentIntraopVolume.GetID())
    self.onQuickSegmentationButtonClicked()

  def inputsAreSet(self):
    return not (self.preopVolumeSelector.currentNode() is None and self.intraopVolumeSelector.currentNode() is None and
                self.preopLabelSelector.currentNode() is None and self.intraopLabelSelector.currentNode() is None and
                self.fiducialSelector.currentNode() is None)

  def updateCurrentPatientAndViewBox(self, currentFile):
    self.currentID = self.logic.getDICOMValue(currentFile, DICOMTAGS.PATIENT_ID)
    self.patientID.setText(self.currentID)

    def updatePreopStudyDate():
      preopStudyDateDICOM = self.logic.getDICOMValue(currentFile, DICOMTAGS.STUDY_DATE)
      formattedDate = preopStudyDateDICOM[0:4] + "-" + preopStudyDateDICOM[4:6] + "-" + \
                      preopStudyDateDICOM[6:8]
      self.preopStudyDate.setText(formattedDate)

    def updateCurrentStudyDate():
      currentStudyDate = qt.QDate().currentDate()
      self.currentStudyDate.setText(str(currentStudyDate))

    def updatePatientBirthDate():
      currentBirthDateDICOM = self.logic.getDICOMValue(currentFile, DICOMTAGS.PATIENT_BIRTH_DATE)
      if currentBirthDateDICOM is None:
        self.patientBirthDate.setText('No Date found')
      else:
        currentBirthDateDICOM = str(currentBirthDateDICOM)
        self.currentBirthDate = currentBirthDateDICOM[0:4] + "-" + \
                                currentBirthDateDICOM[4:6] + "-" + \
                                currentBirthDateDICOM[6:8]
        self.patientBirthDate.setText(self.currentBirthDate)

    def updatePatientName():
      self.currentPatientName = None
      currentPatientNameDICOM = self.logic.getDICOMValue(currentFile, DICOMTAGS.PATIENT_NAME)
      if currentPatientNameDICOM:
        splitted = currentPatientNameDICOM.split('^')
        try:
          self.currentPatientName = splitted[1] + ", " + splitted[0]
        except IndexError:
          self.currentPatientName = splitted[0]
      self.patientName.setText(self.currentPatientName)

    updatePatientBirthDate()
    updateCurrentStudyDate()
    updatePreopStudyDate()
    updatePatientName()

  def updateSeriesSelectorTable(self):
    self.intraopSeriesSelector.clear()
    seriesList = self.logic.seriesList
    for series in seriesList:
      sItem = qt.QStandardItem(series)
      self.seriesItems.append(sItem)
      self.seriesModel.appendRow(sItem)
      color = COLOR.YELLOW
      if self.registrationResults.registrationResultWasApproved(series) or \
        (self.COVER_TEMPLATE in series and self.logic.zFrameRegistrationSuccessful):
        color = COLOR.GREEN
      elif self.registrationResults.registrationResultWasSkipped(series) or self.wasSeriesSkipped(series):
        color = COLOR.RED
      elif self.registrationResults.registrationResultWasRejected(series):
        color = COLOR.GRAY
      self.seriesModel.setData(sItem.index(), color, qt.Qt.BackgroundRole)

    substring = self.GUIDANCE_IMAGE
    if not self.registrationResults.getMostRecentApprovedCoverProstateRegistration():
      substring = self.COVER_TEMPLATE if not self.logic.zFrameRegistrationSuccessful else self.COVER_PROSTATE

    for item in list(reversed(range(len(seriesList)))):
      series = self.seriesModel.item(item).text()
      if substring in series:
        index = self.intraopSeriesSelector.findText(series)
        self.intraopSeriesSelector.setCurrentIndex(index)
        break
    self.onIntraopSeriesSelectionChanged()

  def resetShowResultButtons(self, checkedButton):
    checked = STYLE.GRAY_BACKGROUND_WHITE_FONT
    unchecked = STYLE.WHITE_BACKGROUND
    for button in self.registrationButtonGroup.buttons():
      button.setStyleSheet(checked if button is checkedButton else unchecked)

  def onRegistrationResultSelected(self, seriesText):
    if not seriesText:
      return
    self.hideAllTargets()
    self.currentResult = seriesText
    self.showAffineResultButton.setEnabled(self.GUIDANCE_IMAGE not in seriesText)
    self.onRegistrationButtonChecked(self.activeRegistrationResultButtonId)

  def wasSeriesSkipped(self, series):
    return series in self.skippedIntraopSeries

  def hideAllTargets(self):
    for result in self.registrationResults.getResultsAsList():
      for targetNode in [targets for targets in result.targets.values() if targets]:
        self.setTargetVisibility(targetNode, show=False)
    self.setTargetVisibility(self.preopTargets, show=False)

  def deselectAllTargets(self):
    for result in self.registrationResults.getResultsAsList():
      for targetNode in [targets for targets in result.targets.values() if targets]:
        self.setTargetSelected(targetNode)
    self.setTargetSelected(self.preopTargets)

  def onRigidResultClicked(self):
    self.displayRegistrationResults(button=self.showRigidResultButton, registrationType='rigid')
    self.model.targetList = self.currentResult.rigidTargets

  def onAffineResultClicked(self):
    self.displayRegistrationResults(button=self.showAffineResultButton, registrationType='affine')
    self.model.targetList = self.currentResult.affineTargets

  def onBSplineResultClicked(self):
    self.displayRegistrationResults(button=self.showBSplineResultButton, registrationType='bSpline')
    self.model.targetList = self.currentResult.bSplineTargets

  def displayRegistrationResults(self, button, registrationType):
    self.resetShowResultButtons(checkedButton=button)
    self.setCurrentRegistrationResultSliceViews(registrationType)
    self.showTargets(registrationType=registrationType)
    self.visualEffectsGroupBox.setEnabled(True)
    self.jumpToTarget()

  def setDefaultFOV(self, sliceLogic):
    sliceLogic.FitSliceToAll()
    FOV = sliceLogic.GetSliceNode().GetFieldOfView()
    self.setFOV(sliceLogic, [FOV[0] * 0.5, FOV[1] * 0.5, FOV[2]])

  def setFOV(self, sliceLogic, FOV):
    sliceNode = sliceLogic.GetSliceNode()
    sliceLogic.StartSliceNodeInteraction(2)
    sliceNode.SetFieldOfView(FOV[0], FOV[1], FOV[2])
    sliceLogic.EndSliceNodeInteraction()

  def setCurrentRegistrationResultSliceViews(self, registrationType):
    for compositeNode in [self.redCompositeNode, self.slice4CompositeNode]:
      compositeNode.SetBackgroundVolumeID(self.preopVolume.GetID())
      compositeNode.SetForegroundVolumeID(None)

    for compositeNode in [self.yellowCompositeNode, self.slice5CompositeNode]:
      compositeNode.SetForegroundVolumeID(self.currentResult.fixedVolume.GetID())
      compositeNode.SetBackgroundVolumeID(self.currentResult.getVolume(registrationType).GetID())

    for compositeNode in [self.slice4CompositeNode, self.slice5CompositeNode, self.greenCompositeNode]:
      compositeNode.SetLabelOpacity(0)
    self.greenCompositeNode.SetForegroundVolumeID(None)
    self.greenCompositeNode.SetBackgroundVolumeID(None)
    self.setDefaultFOV(self.redSliceLogic)
    self.setDefaultFOV(self.yellowSliceLogic)

  def showTargets(self, registrationType):
    self.setTargetVisibility(self.currentResult.rigidTargets, show=registrationType == 'rigid')
    self.setTargetVisibility(self.currentResult.bSplineTargets, show=registrationType == 'bSpline')
    if self.currentResult.affineTargets:
      self.setTargetVisibility(self.currentResult.affineTargets, show=registrationType == 'affine')
    self.currentTargets = getattr(self.currentResult, registrationType+'Targets')
    self.setTargetVisibility(self.preopTargets)

  def setTargetVisibility(self, targetNode, show=True):
    self.markupsLogic.SetAllMarkupsVisibility(targetNode, show)

  def setTargetSelected(self, targetNode, selected=False):
    self.markupsLogic.SetAllMarkupsSelected(targetNode, selected)

  def configureSliceNodesForPreopData(self):
    for nodeId in ["vtkMRMLSliceNodeRed", "vtkMRMLSliceNodeYellow", "vtkMRMLSliceNodeGreen"]:
      slicer.mrmlScene.GetNodeByID(nodeId).SetUseLabelOutline(True)
    self.redSliceNode.SetOrientationToAxial()

  def loadT2Label(self):
    mostRecentFilename = self.logic.getMostRecentWholeGlantSegmentation(self.preopSegmentationPath)
    success = False
    if mostRecentFilename:
      filename = os.path.join(self.preopSegmentationPath, mostRecentFilename)
      (success, self.preopLabel) = slicer.util.loadLabelVolume(filename, returnNode=True)
      if success:
        self.preopLabel.SetName('t2-label')
        displayNode = self.preopLabel.GetDisplayNode()
        displayNode.SetAndObserveColorNodeID('vtkMRMLColorTableNode1')
        # rotate volume to plane
        for nodeId in ["vtkMRMLSliceNodeRed", "vtkMRMLSliceNodeYellow", "vtkMRMLSliceNodeGreen"]:
          slicer.mrmlScene.GetNodeByID(nodeId).RotateToVolumePlane(self.preopLabel)
        self.preopLabelSelector.setCurrentNode(self.preopLabel)
    return success

  def loadPreopVolume(self):
    success, self.preopVolume = slicer.util.loadVolume(self.preopImagePath, returnNode=True)
    if success:
      self.preopVolume.SetName('volume-PREOP')
      self.preopVolumeSelector.setCurrentNode(self.preopVolume)
    return success

  def loadPreopTargets(self):
    mostRecentTargets = self.logic.getMostRecentTargetsFile(self.preopTargetsPath)
    success = False
    if mostRecentTargets:
      filename = os.path.join(self.preopTargetsPath, mostRecentTargets)
      success, self.preopTargets = slicer.util.loadMarkupsFiducialList(filename, returnNode=True)
      if success:
        self.preopTargets.SetName('targets-PREOP')
    return success

  def loadMpReviewProcessedData(self, preopDir):
    resourcesDir = os.path.join(preopDir, 'RESOURCES')
    self.preopTargetsPath = os.path.join(preopDir, 'Targets')

    if not os.path.exists(resourcesDir):
      self.confirmDialog("The selected directory does not fit the mpReview directory structure. Make sure that you "
                         "select the study root directory which includes directories RESOURCES")
      return False

    seriesMap = {}

    patientInformationRetrieved = False

    for root, subdirs, files in os.walk(resourcesDir):
      logging.debug('Root: ' + root + ', files: ' + str(files))
      resourceType = os.path.split(root)[1]

      logging.debug('Resource: ' + resourceType)

      if resourceType == 'Reconstructions':
        for f in [f for f in files if f.endswith('.xml')]:
          logging.debug('File: ' + f)
          metaFile = os.path.join(root, f)
          logging.debug('Ends with xml: ' + metaFile)
          try:
            (seriesNumber, seriesName) = self.logic.getSeriesInfoFromXML(metaFile)
            logging.debug(str(seriesNumber) + ' ' + seriesName)
          except:
            logging.debug('Failed to get from XML')
            continue

          volumePath = os.path.join(root, seriesNumber + '.nrrd')
          seriesMap[seriesNumber] = {'MetaInfo': None, 'NRRDLocation': volumePath, 'LongName': seriesName}
          seriesMap[seriesNumber]['ShortName'] = str(seriesNumber) + ":" + seriesName
      elif resourceType == 'DICOM' and not patientInformationRetrieved:
        self.logic.importStudy(root)
        for f in files:
          self.updateCurrentPatientAndViewBox(os.path.join(root, f))
          patientInformationRetrieved = True
          break

    logging.debug('All series found: ' + str(seriesMap.keys()))
    logging.debug('All series found: ' + str(seriesMap.values()))

    logging.debug('******************************************************************************')

    self.preopImagePath = ''
    self.preopSegmentationPath = ''

    for series in seriesMap:
      seriesName = str(seriesMap[series]['LongName'])
      logging.debug('series Number ' + series + ' ' + seriesName)
      if re.search("ax", str(seriesName), re.IGNORECASE) and re.search("t2", str(seriesName), re.IGNORECASE):
        logging.debug(' FOUND THE SERIES OF INTEREST, ITS ' + seriesName)
        logging.debug(' LOCATION OF VOLUME : ' + str(seriesMap[series]['NRRDLocation']))

        path = os.path.join(seriesMap[series]['NRRDLocation'])
        logging.debug(' LOCATION OF IMAGE path : ' + str(path))

        segmentationPath = os.path.dirname(os.path.dirname(path))
        segmentationPath = os.path.join(segmentationPath, 'Segmentations')
        logging.debug(' LOCATION OF SEGMENTATION path : ' + segmentationPath)

        if not os.path.exists(segmentationPath):
          self.confirmDialog("No segmentations found.\nMake sure that you used mpReview for segmenting the prostate "
                             "first and using its output as the preop data input here.")
          return False
        self.preopImagePath = seriesMap[series]['NRRDLocation']
        self.preopSegmentationPath = segmentationPath
        break

    return True

  def loadPreopData(self):
    # TODO: using decorators
    if not self.loadMpReviewProcessedData(self.preopDataDir):
      return
    self.configureSliceNodesForPreopData()
    if not self.loadT2Label() or not self.loadPreopVolume() or not self.loadPreopTargets():
      self.warningDialog("Loading preop data failed.\nMake sure that the correct directory structure like mpReview "
                         "explains is used. SliceTracker expects a volume, label and target")
      self.intraopDirButton.setEnabled(False)
      return
    else:
      self.intraopDirButton.setEnabled(True)
    if self.yesNoDialog("Was an endorectal coil used for preop image acquisition?"):
      progress = self.makeProgressIndicator(2, 1)
      progress.labelText = '\nBias Correction'
      self.preopVolume = self.logic.applyBiasCorrection(self.preopVolume, self.preopLabel)
      progress.setValue(2)
      progress.close()
      self.preopVolumeSelector.setCurrentNode(self.preopVolume)
      self.logic.biasCorrectionDone = True
    else:
      self.logic.biasCorrectionDone = False
    logging.debug('TARGETS PREOP')
    logging.debug(self.preopTargets)

    self.setTargetVisibility(self.preopTargets, show=True)
    self.model.targetList = self.preopTargets

    self.fiducialSelector.setCurrentNode(self.preopTargets)
    self.markupsLogic.JumpSlicesToNthPointInMarkup(self.preopTargets.GetID(), 0)

    self.logic.styleDisplayNode(self.preopTargets.GetDisplayNode())
    self.redCompositeNode.SetLabelOpacity(1)

    self.layoutManager.setLayout(self.LAYOUT_RED_SLICE_ONLY)

    self.setDefaultFOV(self.redSliceLogic)

  def patientCheckAfterImport(self, fileList):
    for currentFile in fileList:
      currentFile = os.path.join(self.intraopDataDir, currentFile)
      patientID = self.logic.getDICOMValue(currentFile, DICOMTAGS.PATIENT_ID)
      if patientID != self.currentID and patientID is not None:
        if not self.yesNoDialog(message='WARNING: Preop data of Patient ID ' + self.currentID + ' was selected, but '
                                        ' data of patient with ID ' + patientID + ' just arrived in the folder, which '
                                        'you selected for incoming data.\nDo you still want to continue?',
                                title="Patients Not Matching"):
          self.intraopSeriesSelector.clear()
          return
        else:
          break
    self.updateSeriesSelectorTable()

  def onCancelSegmentationButtonClicked(self):
    if self.yesNoDialog("Do you really want to cancel the segmentation process?"):
      self.setQuickSegmentationModeOFF()

  def onQuickSegmentationButtonClicked(self):
    self.hideAllLabels()
    self.setBackgroundToVolume(self.referenceVolumeSelector.currentNode().GetID())
    self.setQuickSegmentationModeON()

  def setBackgroundToVolume(self, volumeID):
    for compositeNode in [self.redCompositeNode, self.yellowCompositeNode, self.greenCompositeNode]:
      compositeNode.Reset()
      compositeNode.SetBackgroundVolumeID(volumeID)
    self.setDefaultOrientation()

  def hideAllLabels(self):
    for compositeNode in [self.redCompositeNode, self.yellowCompositeNode, self.greenCompositeNode]:
      compositeNode.SetLabelVolumeID(None)

  def setQuickSegmentationModeON(self):
    self.logic.deleteClippingData()
    self.setSegmentationButtons(segmentationActive=True)
    self.deactivateUndoRedoButtons()
    self.disableEditorWidgetAndResetEditorTool()
    self.setupQuickModeHistory()
    self.layoutManager.setLayout(self.LAYOUT_FOUR_UP)
    self.logic.runQuickSegmentationMode()
    # TODO: remove Observer after segmentation finised
    self.logic.inputMarkupNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.updateUndoRedoButtons)

  def disableEditorWidgetAndResetEditorTool(self, enabledButton=False):
    self.editorWidgetParent.hide()
    self.editorParameterNode.SetParameter('effect', 'DefaultTool')
    self.editorWidgetButton.setEnabled(enabledButton)

  def setQuickSegmentationModeOFF(self):
    self.setSegmentationButtons(segmentationActive=False)
    self.deactivateUndoRedoButtons()
    self.resetToRegularViewMode()

  def setSegmentationButtons(self, segmentationActive=False):
    self.quickSegmentationButton.setEnabled(not segmentationActive)
    self.applySegmentationButton.setEnabled(segmentationActive)
    self.cancelSegmentationButton.setEnabled(segmentationActive)

  def setupQuickModeHistory(self):
    try:
      self.deletedMarkups.Reset()
    except AttributeError:
      self.deletedMarkups = slicer.vtkMRMLMarkupsFiducialNode()
      self.deletedMarkups.SetName('deletedMarkups')

  def resetToRegularViewMode(self):
    interactionNode = slicer.mrmlScene.GetNodeByID("vtkMRMLInteractionNodeSingleton")
    interactionNode.SwitchToViewTransformMode()
    interactionNode.SetPlaceModePersistence(0)

  def changeOpacity(self, value):
    self.yellowCompositeNode.SetForegroundOpacity(value)
    self.slice5CompositeNode.SetForegroundOpacity(value)
    self.setOldNewIndicatorAnnotationOpacity(value)

  def openEvaluationStep(self):
    self.evaluationModeOn = True
    self.currentRegisteredSeries.setText(self.logic.currentIntraopVolume.GetName())
    self.targetingGroupBox.hide()
    self.registrationEvaluationGroupBoxLayout.addWidget(self.targetTable, 4, 0)
    self.registrationEvaluationGroupBox.show()

  def connectCrosshairNode(self):
    if not self.crosshairNodeObserverTag:
      self.crosshairNodeObserverTag = self.crosshairNode.AddObserver(slicer.vtkMRMLCrosshairNode.CursorPositionModifiedEvent,
                                                                     self.calcCursorTargetsDistance)

  def disconnectCrosshairNode(self):
    if self.crosshairNode and self.crosshairNodeObserverTag:
      self.crosshairNode.RemoveObserver(self.crosshairNodeObserverTag)
    self.crosshairNodeObserverTag = None

  def calcCursorTargetsDistance(self, observee, event):
    ras = [0.0,0.0,0.0]
    xyz = [0.0,0.0,0.0]
    insideView = self.crosshairNode.GetCursorPositionRAS(ras)
    sliceNode = self.crosshairNode.GetCursorPositionXYZ(xyz)
    if sliceNode is not self.yellowSliceNode or not insideView:
       self.model.cursorPosition = None
    else:
       self.model.cursorPosition = ras

  def openTargetingStep(self, ratingResult=None):
    self.zFrameRegistrationGroupBox.hide()
    self.evaluationModeOn = False
    self.save()
    self.disconnectCrosshairNode()
    self.activeRegistrationResultButtonId = 3
    self.hideAllLabels()
    if ratingResult:
      self.currentResult.score = ratingResult
    self.registrationWatchBox.hide()
    self.updateSeriesSelectorTable()
    self.registrationEvaluationGroupBox.hide()
    self.targetingGroupBoxLayout.addWidget(self.targetTable, 1, 0, 1, 2)
    self.targetingGroupBox.show()
    self.removeSliceAnnotations()
    self.uncheckVisualEffects()

  def onApproveRegistrationResultButtonClicked(self):
    self.currentResult.approve()

    if self.ratingWindow.isRatingEnabled():
      self.ratingWindow.show(disableWidget=self.parent, callback=self.openTargetingStep)
    else:
      self.openTargetingStep()

  def onSkipRegistrationResultButtonClicked(self):
    self.currentResult.skip()
    self.openTargetingStep()

  def onSkipIntraopSeriesButtonClicked(self):
    self.skippedIntraopSeries.append(self.intraopSeriesSelector.currentText)
    self.updateSeriesSelectorTable()

  def onRejectRegistrationResultButtonClicked(self):
    results = self.registrationResults.getResultsBySeriesNumber(self.currentResult.seriesNumber)
    for result in results:
      result.reject()
    self.openTargetingStep()

  def updateRegistrationEvaluationButtons(self):
    results = self.registrationResults.getResultsBySeriesNumber(self.currentResult.seriesNumber)
    self.rejectRegistrationResultButton.setEnabled(len(results) > 1)
    self.skipRegistrationResultButton.setEnabled(len(results) == 1)

  def onApplySegmentationButtonClicked(self):
    self.setAxialOrientation()
    self.onQuickSegmentationFinished()

  def onQuickSegmentationFinished(self):
    inputVolume = self.referenceVolumeSelector.currentNode()
    continueSegmentation = False
    if self.logic.inputMarkupNode.GetNumberOfFiducials() > 3 and self.validPointsForQuickModeSet():

      self.currentIntraopLabel = self.logic.labelMapFromClippingModel(inputVolume)
      labelName = self.referenceVolumeSelector.currentNode().GetName() + '-label'
      self.currentIntraopLabel.SetName(labelName)

      displayNode = self.currentIntraopLabel.GetDisplayNode()
      displayNode.SetAndObserveColorNodeID('vtkMRMLColorTableNode1')

      self.intraopLabelSelector.setCurrentNode(self.currentIntraopLabel)

      self.setTargetVisibility(self.logic.inputMarkupNode, show=False)
      self.logic.clippingModelNode.SetDisplayVisibility(False)
      self.setupScreenAfterSegmentation()
    else:
      if self.yesNoDialog("You need to set at least three points with an additional one situated on a distinct slice "
                          "as the algorithm input in order to be able to create a proper segmentation. This step is "
                          "essential for an efficient registration. Do you want to continue using the quick mode?"):
        continueSegmentation = True
      else:
        self.logic.deleteClippingData()
    if not continueSegmentation:
      self.setQuickSegmentationModeOFF()
      self.setSegmentationButtons(segmentationActive=False)

  def validPointsForQuickModeSet(self):
    positions = self.getMarkupSlicePositions()
    return min(positions) != max(positions)

  def getMarkupSlicePositions(self):
    markupNode = self.logic.inputMarkupNode
    nOfControlPoints = markupNode.GetNumberOfFiducials()
    positions = []
    pos = [0, 0, 0]
    for i in range(nOfControlPoints):
      markupNode.GetNthFiducialPosition(i, pos)
      positions.append(pos[2])
    return positions

  def setupScreenAfterSegmentation(self):
    self.hideAllLabels()
    self.hideAllTargets()
    self.layoutManager.setLayout(self.LAYOUT_SIDE_BY_SIDE)

    if self.logic.retryMode:
      coverProstateRegResult = self.registrationResults.getMostRecentApprovedCoverProstateRegistration()
      if coverProstateRegResult:
        self.preopVolumeSelector.setCurrentNode(coverProstateRegResult.fixedVolume)
        self.preopLabelSelector.setCurrentNode(coverProstateRegResult.fixedLabel)
        self.fiducialSelector.setCurrentNode(coverProstateRegResult.bSplineTargets)

    preopVolume = self.preopVolumeSelector.currentNode()
    preopLabel = self.preopLabelSelector.currentNode()
    intraopVolume = self.intraopVolumeSelector.currentNode()
    intraopLabel = self.intraopLabelSelector.currentNode()

    self.setupScreenForSegmentationComparison("red", preopVolume, preopLabel)
    self.setupScreenForSegmentationComparison("yellow", intraopVolume, intraopLabel)
    self.applyRegistrationButton.setEnabled(1 if self.inputsAreSet() else 0)
    self.editorWidgetButton.setEnabled(True)
    self.registrationWatchBox.show()

  def setupScreenForSegmentationComparison(self, viewName, volume, label):
    compositeNode = getattr(self, viewName+"CompositeNode")
    compositeNode.SetReferenceBackgroundVolumeID(volume.GetID())
    compositeNode.SetLabelVolumeID(label.GetID())
    compositeNode.SetLabelOpacity(1)
    logic = getattr(self, viewName+"SliceLogic")
    logic.GetSliceNode().RotateToVolumePlane(volume)
    logic.FitSliceToAll()
    logic.GetSliceNode().SetFieldOfView(86, 136, 3.5)

  def onTrackTargetsButtonClicked(self):
    self.removeSliceAnnotations()
    if not self.logic.zFrameRegistrationSuccessful and self.COVER_TEMPLATE in self.intraopSeriesSelector.currentText:
      self.openZFrameRegistrationStep()
      return
    else:
      if self.currentResult is None or \
         self.registrationResults.getMostRecentApprovedCoverProstateRegistration() is None or \
         self.logic.retryMode or self.COVER_PROSTATE in self.intraopSeriesSelector.currentText:
        self.initiateOrRetryTracking()
      else:
        self.repeatRegistrationForCurrentSelection()
      self.openEvaluationStep()

  def openZFrameRegistrationStep(self):
    volume = self.logic.loadSeriesIntoSlicer(self.intraopSeriesSelector.currentText, clearOldSeries=True)
    if volume:
      self.evaluationModeOn = True
      self.targetingGroupBox.hide()
      self.zFrameRegistrationGroupBox.show()
      progress = self.makeProgressIndicator(2, 1)
      progress.labelText = '\nZFrame registration'
      self.logic.runZFrameRegistration(volume)
      progress.setValue(2)
      progress.close()
      self.layoutManager.setLayout(self.LAYOUT_FOUR_UP)
      self.setDefaultOrientation()
      self.redSliceNode.SetSliceVisible(True)
      self.showZFrameModelCheckbox.checked = True

  def onApproveZFrameRegistrationButtonClicked(self):
    self.logic.zFrameRegistrationSuccessful = True
    self.redSliceNode.SetSliceVisible(False)
    self.openTargetingStep()

  def initiateOrRetryTracking(self):
    volume = self.logic.loadSeriesIntoSlicer(self.intraopSeriesSelector.currentText, clearOldSeries=True)
    if volume:
      self.logic.currentIntraopVolume = volume
    self.disableTargetTable()
    self.segmentationGroupBox.show()
    self.editorWidgetButton.setEnabled(False)
    self.activateRegistrationResultsArea(collapsed=True, enabled=False)
    self.registrationWatchBox.hide()
    self.configureSegmentationMode()

  def activateRegistrationResultsArea(self, collapsed, enabled):
    self.collapsibleRegistrationArea.collapsed = collapsed
    self.collapsibleRegistrationArea.enabled = enabled

  def disableTargetTable(self):
    self.hideAllTargets()
    self.targetTable.clearSelection()
    self.targetTable.enabled = False

  def repeatRegistrationForCurrentSelection(self):
    logging.debug('Performing Re-Registration')
    volume = self.logic.loadSeriesIntoSlicer(self.intraopSeriesSelector.currentText)
    if volume:
      self.logic.currentIntraopVolume = volume
    self.onInvokeRegistration(initial=False)
    self.segmentationGroupBox.hide()
    self.activateRegistrationResultsArea(collapsed=False, enabled=True)

  def onEditorGearIconClicked(self):
    if self.editorWidgetParent.visible:
      self.disableEditorWidgetAndResetEditorTool(enabledButton=True)
    else:
      self.editorWidgetParent.show()
      displayNode = self.currentIntraopLabel.GetDisplayNode()
      displayNode.SetAndObserveColorNodeID('vtkMRMLColorTableNode1')
      self.editorParameterNode.SetParameter('effect', 'DrawEffect')
      self.editUtil.setLabel(8)
      self.editUtil.setLabelOutline(1)

  def onInvokeRegistration(self, initial=True):
    self.disableEditorWidgetAndResetEditorTool()
    self.applyRegistrationButton.setEnabled(False)
    self.progress = self.makeProgressIndicator(4, 1)
    if initial:
      self.logic.applyInitialRegistration(fixedVolume=self.intraopVolumeSelector.currentNode(),
                                          sourceVolume=self.preopVolumeSelector.currentNode(),
                                          fixedLabel=self.intraopLabelSelector.currentNode(),
                                          movingLabel=self.preopLabelSelector.currentNode(),
                                          targets=self.fiducialSelector.currentNode(),
                                          progressCallback=self.updateProgressBar)
    else:
      self.logic.applyRegistration(progressCallback=self.updateProgressBar)
    self.progress.close()
    self.progress = None
    self.finalizeRegistrationStep()
    self.registrationGroupBox.hide()
    logging.debug('Re-Registration is done')

  def updateProgressBar(self, **kwargs):
    if self.progress:
      for key, value in kwargs.iteritems():
        if hasattr(self.progress, key):
          setattr(self.progress, key, value)

  def onRetryRegistrationButtonClicked(self):
    self.logic.retryMode = True
    self.evaluationButtonsGroupBox.enabled = False
    self.onTrackTargetsButtonClicked()

  def finalizeRegistrationStep(self):
    self.targetTable.enabled = True
    self.addNewTargetsToScene()
    self.activeRegistrationResultButtonId = 3
    self.updateRegistrationResultSelector()
    self.setupRegistrationResultView()
    self.onBSplineResultClicked()
    self.organizeUIAfterRegistration()
    self.currentResult.printSummary()
    self.connectCrosshairNode()

  def addNewTargetsToScene(self):
    for targetNode in [targets for targets in self.currentResult.targets.values() if targets]:
      slicer.mrmlScene.AddNode(targetNode)

  def setupRegistrationResultView(self):
    self.hideAllLabels()
    self.addSliceAnnotations()
    slicer.app.applicationLogic().FitSliceToAll()

    self.refreshViewNodeIDs(self.preopTargets, self.redSliceNode)
    for targetNode in [targets for targets in self.currentResult.targets.values() if targets]:
      self.refreshViewNodeIDs(targetNode, self.yellowSliceNode)

    self.resetToRegularViewMode()

    self.layoutManager.setLayout(self.LAYOUT_SIDE_BY_SIDE)

    self.setAxialOrientation()
    self.setSagittalOrientation()

  def organizeUIAfterRegistration(self):
    self.registrationWatchBox.show()
    self.segmentationGroupBox.hide()
    self.activateRegistrationResultsArea(collapsed=False, enabled=True)
    self.evaluationButtonsGroupBox.enabled = True
    self.updateRegistrationEvaluationButtons()

  def refreshViewNodeIDs(self, targets, sliceNode):
    displayNode = targets.GetDisplayNode()
    displayNode.RemoveAllViewNodeIDs()
    displayNode.AddViewNodeID(sliceNode.GetID())

  def onNewImageDataReceived(self, **kwargs):
    newFileList = kwargs.pop('newList')
    self.patientCheckAfterImport(newFileList)
    if self.evaluationModeOn is True:
      return
    if self.notifyUserAboutNewData and any(seriesText in self.intraopSeriesSelector.currentText for seriesText
                                           in [self.COVER_TEMPLATE, self.COVER_PROSTATE, self.GUIDANCE_IMAGE]):
      dialog = IncomingDataMessageBox()
      answer, checked = dialog.exec_()
      self.notifyUserAboutNewData = not checked
      if answer == qt.QMessageBox.AcceptRole:
        self.onTrackTargetsButtonClicked()


class SliceTrackerLogic(ScriptedLoadableModuleLogic, ModuleLogicMixin):

  ZFRAME_MODEL_PATH = 'Resources/zframe/zframe-model.vtk'

  @property
  def preopDataDir(self):
    return self._preopDataDir

  @preopDataDir.setter
  def preopDataDir(self, path):
    if os.path.exists(path):
      self._preopDataDir = path

  @property
  def intraopDataDir(self):
    return self._intraopDataDir

  @intraopDataDir.setter
  def intraopDataDir(self, path):
    if os.path.exists(path):
      self._intraopDataDir = path
      self.startIntraopDirListener()
      self.startStoreSCP()

  @property
  def outputDir(self):
    return self._outputDir

  @outputDir.setter
  def outputDir(self, path):
    self._outputDir = path

  @property
  def currentResult(self):
      return self.registrationResults.activeResult

  @currentResult.setter
  def currentResult(self, series):
    self.registrationResults.activeResult = series

  def __init__(self, parent=None):
    ScriptedLoadableModuleLogic.__init__(self, parent)
    self.modulePath = os.path.dirname(slicer.util.modulePath(self.moduleName))
    self.inputMarkupNode = None
    self.clippingModelNode = None
    self.seriesList = []
    self.loadableList = {}
    self.alreadyLoadedSeries = {}
    self.storeSCPProcess = None

    self.currentIntraopVolume = None
    self.registrationResults = RegistrationResults()

    self._preopDataDir = ""
    self._intraopDataDir = ""
    self._outputDir = ""

    self._incomingDataCallback = None

    self.biasCorrectionDone = False

    self.volumesLogic = slicer.modules.volumes.logic()
    self.retryMode = False
    self.zFrameRegistrationSuccessful = False
    self.zFrameModelNode = None
    self.zFrameModelNodeID = ''
    self.loadZFrameModel()

    self.showTemplatePath = False
    self.showNeedlePath = False

    self.needleModelNode = None
    self.tempModelNode = None
    self.pathModelNode = None
    self.templateConfig = []
    self.templateMaxDepth = []
    self.pathOrigins = []  ## Origins of needle paths (after transformation by parent transform node)
    self.pathVectors = []  ## Normal vectors of needle paths (after transformation by parent transform node)

  def __del__(self):
    if self.storeSCPProcess:
      self.storeSCPProcess.kill()
    del self.registrationResults

  def isTrackingPossible(self, series):
    return not (self.registrationResults.registrationResultWasApproved(series) or
                self.registrationResults.registrationResultWasSkipped(series) or
                self.registrationResults.registrationResultWasRejected(series))

  def setReceivedNewImageDataCallback(self, func):
    assert hasattr(func, '__call__')
    self._incomingDataCallback = func

  def save(self):
    # TODO: if registration was redone: make a sub folder and move all initial results there
    self.createDirectory(self._outputDir)

    successfullySavedData = ["The following data was successfully saved:\n"]
    failedSaveOfData = ["The following data failed to saved:\n"]

    def saveNodeData(node, extension, name=None):
      try:
        name = name if name else node.GetName()
        name = replaceUnwantedCharacters(name)
        filename = os.path.join(self._outputDir, name + extension)
        success = slicer.util.saveNode(node, filename)
        listToAdd = successfullySavedData if success else failedSaveOfData
        listToAdd.append(node.GetName())
      except AttributeError:
        failedSaveOfData.append(name)

    def replaceUnwantedCharacters(string, characters=None, replaceWith="-"):
      if not characters:
        characters = [": ", " ", ":", "/"]
      for character in characters:
        string = string.replace(character, replaceWith)
      return string

    def saveIntraopSegmentation():
      intraopLabel = self.registrationResults.intraopLabel
      if intraopLabel:
        intraopLabelName = intraopLabel.GetName().replace("label", "LABEL")
        saveNodeData(intraopLabel, '.nrrd', name=intraopLabelName)
        modelName = intraopLabel.GetName().replace("label", "MODEL")
        if self.clippingModelNode:
          saveNodeData(self.clippingModelNode, '.vtk', name=modelName)

    def saveOriginalTargets():
      originalTargets = self.registrationResults.originalTargets
      if originalTargets:
        saveNodeData(originalTargets, '.fcsv', name="PreopTargets")

    def saveBiasCorrectionResult():
      if self.biasCorrectionDone:
        biasCorrectedResult = self.registrationResults.biasCorrectedResult
        if biasCorrectedResult:
          saveNodeData(biasCorrectedResult, '.nrrd')

    def saveRegistrationResults():
      saveRegistrationCommandLineArguments()
      saveOutputTransformations()
      saveTransformedTargets()
      saveTipPosition()

    def saveRegistrationCommandLineArguments():
      for result in self.registrationResults.getResultsAsList():
        name = replaceUnwantedCharacters(result.name)
        filename = os.path.join(self._outputDir, name + "-CMD-PARAMETERS.txt")
        f = open(filename, 'w+')
        f.write(result.cmdArguments)
        f.close()

    def saveOutputTransformations():
      for result in self.registrationResults.getResultsAsList():
        for transformNode in [node for node in result.transforms.values() if node]:
          saveNodeData(transformNode, ".h5")

    def saveTransformedTargets():
      for result in self.registrationResults.getResultsAsList():
        for targetNode in [node for node in result.targets.values() if node]:
          saveNodeData(targetNode, ".fcsv")

    def saveTipPosition():
      for result in self.registrationResults.getResultsAsList():
        if result.tipPosition:
          # TODO: implement
          # prefixed with the series number
          pass

    saveIntraopSegmentation()
    saveOriginalTargets()
    saveBiasCorrectionResult()
    saveRegistrationResults()

    messageOutput = ""
    for messageList in [successfullySavedData, failedSaveOfData] :
      if len(messageList) > 1:
        for message in messageList:
          messageOutput += message + "\n"
    return messageOutput if messageOutput != "" else "There is nothing to be saved yet."

  def startStoreSCP(self):
    if self.storeSCPProcess:
      self.storeSCPProcess.kill()
    # command : $ sudo storescp -v -p 104 -od intraopDir
    pathToExe = os.path.join(slicer.app.slicerHome, 'bin', 'storescp')
    self.storeSCPProcess = Popen(["sudo", pathToExe, "-v", "-p", "104", "-od", self._intraopDataDir])
    if not self.storeSCPProcess:
      logging.error("storescp process could not be started. View log messages for further information.")

  def getSeriesInfoFromXML(self, f):
    import xml.dom.minidom
    dom = xml.dom.minidom.parse(f)
    number = self.findElement(dom, 'SeriesNumber')
    name = self.findElement(dom, 'SeriesDescription')
    name = name.replace('-', '')
    name = name.replace('(', '')
    name = name.replace(')', '')
    return number, name

  def findElement(self, dom, name):
    els = dom.getElementsByTagName('element')
    for e in els:
      if e.getAttribute('name') == name:
        return e.childNodes[0].nodeValue

  def getMostRecentWholeGlantSegmentation(self, path):
    return self.getMostRecentFile(path, "nrrd", filter="WholeGland")

  def getMostRecentTargetsFile(self, path):
    return self.getMostRecentFile(path, "fcsv")

  def getMostRecentFile(self, path, fileType, filter=None):
    assert type(fileType) is str
    files = [f for f in os.listdir(path) if f.endswith(fileType)]
    if len(files) == 0:
      return None
    mostRecent = None
    storedTimeStamp = 0
    for filename in files:
      if filter and not filter in filename:
        continue
      actualFileName = filename.split(".")[0]
      timeStamp = int(actualFileName.split("-")[-1])
      if timeStamp > storedTimeStamp:
        mostRecent = filename
        storedTimeStamp = timeStamp
    return mostRecent

  def clearAlreadyLoadedSeries(self):
    for series, volume in self.alreadyLoadedSeries.iteritems():
      print "removing volume %s of series %s " % (volume.GetName(), series)
      # TODO: slicer crash when deleting volumes
      # if slicer.mrmlScene.IsNodePresent(volume):
      #   slicer.mrmlScene.RemoveNode(volume)
    self.alreadyLoadedSeries.clear()

  def applyBiasCorrection(self, volume, label):

    outputVolume = slicer.vtkMRMLScalarVolumeNode()
    outputVolume.SetName('volume-PREOP-N4')
    slicer.mrmlScene.AddNode(outputVolume)
    params = {'inputImageName': volume.GetID(),
              'maskImageName': label.GetID(),
              'outputImageName': outputVolume.GetID(),
              'numberOfIterations': '500,400,300'}

    slicer.cli.run(slicer.modules.n4itkbiasfieldcorrection, None, params, wait_for_completion=True)

    return outputVolume

  def createVolumeAndTransformNodes(self, registrationTypes, prefix, suffix=""):
    for regType in registrationTypes:
      self.currentResult.setVolume(regType, self.createScalarVolumeNode(prefix + '-VOLUME-' + regType + suffix))
      transformName = prefix + '-TRANSFORM-' + regType + suffix
      transform = self.createBSplineTransformNode(transformName) if regType == 'bSpline' \
        else self.createLinearTransformNode(transformName)
      self.currentResult.setTransform(regType, transform)

  def transformTargets(self, registrations, targets, prefix):
    if targets:
      for registration in registrations:
        name = prefix + '-TARGETS-' + registration
        clone = self.cloneFiducialAndTransform(name, targets, self.currentResult.getTransform(registration))
        self.currentResult.setTargets(registration, clone)

  def applyInitialRegistration(self, fixedVolume, sourceVolume, fixedLabel, movingLabel, targets, progressCallback=None):

    self.progressCallback = progressCallback
    if not self.retryMode:
      self.registrationResults = RegistrationResults()
    name, suffix = self.getRegistrationResultNameAndGeneratedSuffix(fixedVolume.GetName())
    result = self.registrationResults.createResult(name+suffix)
    result.fixedVolume = fixedVolume
    result.fixedLabel = fixedLabel
    result.movingLabel = movingLabel
    result.originalTargets = targets
    result.movingVolume = self.volumesLogic.CloneVolume(slicer.mrmlScene, sourceVolume, 'movingVolume-PREOP-INTRAOP')

    self.createVolumeAndTransformNodes(['rigid', 'affine', 'bSpline'], prefix=str(result.seriesNumber), suffix=suffix)

    self.doRigidRegistration(movingBinaryVolume=self.currentResult.movingLabel, initializeTransformMode="useCenterOfROIAlign")
    self.doAffineRegistration()
    self.doBSplineRegistration(initialTransform=self.currentResult.affineTransform, useScaleVersor3D=False,
                               useScaleSkewVersor3D=True,
                               movingBinaryVolume=self.currentResult.movingLabel, useAffine=False, samplingPercentage="0.002",
                               maskInferiorCutOffFromCenter="1000", numberOfHistogramBins="50",
                               numberOfMatchPoints="10", metricSamplingStrategy="Random", costMetric="MMI")
    self.transformTargets(['rigid', 'affine', 'bSpline'], result.originalTargets, str(result.seriesNumber))
    result.movingVolume = sourceVolume
    self.retryMode = False

  def applyRegistration(self, progressCallback=None):

    self.progressCallback = progressCallback

    # TODO: think about retried segmentations
    coverProstateRegResult = self.registrationResults.getMostRecentApprovedCoverProstateRegistration()

    # take the 'intraop label map', which is always fixed label in the very first preop-intraop registration
    lastRigidTfm = self.registrationResults.getLastApprovedRigidTransformation()

    name, suffix = self.getRegistrationResultNameAndGeneratedSuffix(self.currentIntraopVolume.GetName())
    result = self.registrationResults.createResult(name+suffix)
    result.fixedVolume = self.currentIntraopVolume
    result.fixedLabel = self.volumesLogic.CreateAndAddLabelVolume(slicer.mrmlScene, self.currentIntraopVolume,
                                                                  self.currentIntraopVolume.GetName() + '-label')
    result.originalTargets = coverProstateRegResult.bSplineTargets
    sourceVolume = coverProstateRegResult.fixedVolume
    result.movingVolume = self.volumesLogic.CloneVolume(slicer.mrmlScene, sourceVolume, 'movingVolumeReReg')

    self.runBRAINSResample(inputVolume=coverProstateRegResult.fixedLabel, referenceVolume=self.currentIntraopVolume,
                           outputVolume=result.fixedLabel, warpTransform=lastRigidTfm)

    self.createVolumeAndTransformNodes(['rigid', 'bSpline'], prefix=str(result.seriesNumber), suffix=suffix)

    self.doRigidRegistration(initialTransform=lastRigidTfm)
    self.dilateMask(result.fixedLabel)
    self.doBSplineRegistration(initialTransform=self.currentResult.rigidTransform, useScaleVersor3D=True,
                               useScaleSkewVersor3D=True, useAffine=True)

    self.transformTargets(['rigid', 'bSpline'], result.originalTargets, str(result.seriesNumber))
    result.movingVolume = sourceVolume

  def getRegistrationResultNameAndGeneratedSuffix(self, name):
    nOccurences = sum([1 for result in self.registrationResults.getResultsAsList() if name in result.name])
    suffix = ""
    if nOccurences:
      suffix = "_Retry_" + str(nOccurences)
    return name, suffix

  def updateProgress(self, **kwargs):
    if self.progressCallback:
      self.progressCallback(**kwargs)

  def doBSplineRegistration(self, initialTransform, useScaleVersor3D, useScaleSkewVersor3D, **kwargs):
    self.updateProgress(labelText='\nBSpline registration', value=3)
    paramsBSpline = {'fixedVolume': self.currentResult.fixedVolume,
                     'movingVolume': self.currentResult.movingVolume,
                     'outputVolume': self.currentResult.bSplineVolume.GetID(),
                     'bsplineTransform': self.currentResult.bSplineTransform.GetID(),
                     'fixedBinaryVolume': self.currentResult.fixedLabel,
                     'useRigid': False,
                     'useROIBSpline': True,
                     'useBSpline': True,
                     'useScaleVersor3D': useScaleVersor3D,
                     'useScaleSkewVersor3D': useScaleSkewVersor3D,
                     'splineGridSize': "3,3,3",
                     'numberOfIterations': "1500",
                     'maskProcessing': "ROI",
                     'outputVolumePixelType': "float",
                     'backgroundFillValue': "0",
                     'interpolationMode': "Linear",
                     'minimumStepLength': "0.005",
                     'translationScale': "1000",
                     'reproportionScale': "1",
                     'skewScale': "1",
                     'fixedVolumeTimeIndex': "0",
                     'movingVolumeTimeIndex': "0",
                     'medianFilterSize': "0,0,0",
                     'ROIAutoDilateSize': "0",
                     'relaxationFactor': "0.5",
                     'maximumStepLength': "0.2",
                     'failureExitCode': "-1",
                     'numberOfThreads': "-1",
                     'debugLevel': "0",
                     'costFunctionConvergenceFactor': "1.00E+09",
                     'projectedGradientTolerance': "1.00E-05",
                     'maxBSplineDisplacement': "0",
                     'maximumNumberOfEvaluations': "900",
                     'maximumNumberOfCorrections': "25",
                     'removeIntensityOutliers': "0",
                     'ROIAutoClosingSize': "9",
                     'maskProcessingMode': "ROI",
                     'initialTransform': initialTransform}
    for key, value in kwargs.iteritems():
      paramsBSpline[key] = value

    slicer.cli.run(slicer.modules.brainsfit, None, paramsBSpline, wait_for_completion=True)
    self.currentResult.cmdArguments += "BSpline Registration Parameters: %s" % str(paramsBSpline) + "\n\n"

    self.updateProgress(labelText='\nCompleted registration', value=4)

  def doAffineRegistration(self):
    self.updateProgress(labelText='\nAffine registration', value=2)
    paramsAffine = {'fixedVolume': self.currentResult.fixedVolume,
                    'movingVolume': self.currentResult.movingVolume,
                    'fixedBinaryVolume': self.currentResult.fixedLabel,
                    'movingBinaryVolume': self.currentResult.movingLabel,
                    'outputTransform': self.currentResult.affineTransform.GetID(),
                    'outputVolume': self.currentResult.affineVolume.GetID(),
                    'maskProcessingMode': "ROI",
                    'useAffine': True,
                    'initialTransform': self.currentResult.rigidTransform}
    slicer.cli.run(slicer.modules.brainsfit, None, paramsAffine, wait_for_completion=True)
    self.currentResult.cmdArguments += "Affine Registration Parameters: %s" % str(paramsAffine) + "\n\n"

  def doRigidRegistration(self, **kwargs):
    self.updateProgress(labelText='\nRigid registration', value=2)
    paramsRigid = {'fixedVolume': self.currentResult.fixedVolume,
                   'movingVolume': self.currentResult.movingVolume,
                   'fixedBinaryVolume': self.currentResult.fixedLabel,
                   'outputTransform': self.currentResult.rigidTransform.GetID(),
                   'outputVolume': self.currentResult.rigidVolume.GetID(),
                   'maskProcessingMode': "ROI",
                   'useRigid': True,
                   'useAffine': False,
                   'useBSpline': False,
                   'useScaleVersor3D': False,
                   'useScaleSkewVersor3D': False,
                   'useROIBSpline': False}
    for key, value in kwargs.iteritems():
      paramsRigid[key] = value
    slicer.cli.run(slicer.modules.brainsfit, None, paramsRigid, wait_for_completion=True)
    self.currentResult.cmdArguments += "Rigid Registration Parameters: %s" % str(paramsRigid) + "\n\n"

  def cloneFiducialAndTransform(self, cloneName, originalTargets, transformNode):
    tfmLogic = slicer.modules.transforms.logic()
    clonedTargets = self.cloneFiducials(originalTargets, cloneName)
    clonedTargets.SetAndObserveTransformNodeID(transformNode.GetID())
    clonedTargets.SetAndObserveDisplayNodeID(self.displayNode.GetID())
    tfmLogic.hardenTransform(clonedTargets)
    return clonedTargets

  def cloneFiducials(self, original, cloneName):
    mlogic = slicer.modules.markups.logic()
    nodeId = mlogic.AddNewFiducialNode(cloneName, slicer.mrmlScene)
    clone = slicer.mrmlScene.GetNodeByID(nodeId)
    for i in range(original.GetNumberOfFiducials()):
      pos = [0.0, 0.0, 0.0]
      original.GetNthFiducialPosition(i, pos)
      name = original.GetNthFiducialLabel(i)
      clone.AddFiducial(pos[0], pos[1], pos[2])
      clone.SetNthFiducialLabel(i, name)
    return clone

  def dilateMask(self, mask):
    imagedata = mask.GetImageData()
    dilateErode = vtk.vtkImageDilateErode3D()
    dilateErode.SetInputData(imagedata)
    dilateErode.SetDilateValue(1.0)
    dilateErode.SetErodeValue(0.0)
    dilateErode.SetKernelSize(12,12,1)
    dilateErode.Update()
    mask.SetAndObserveImageData(dilateErode.GetOutput())

  def startIntraopDirListener(self):
    numberOfFiles = len(self.getFileList(self._intraopDataDir))
    self.lastFileCount = numberOfFiles
    self.createCurrentFileList(self._intraopDataDir)
    self.startTimer()

  def startTimer(self):
    currentFileCount = len(self.getFileList(self._intraopDataDir))
    if self.lastFileCount != currentFileCount:
      qt.QTimer.singleShot(5000, lambda count=currentFileCount: self.importDICOMSeries(count))
    self.lastFileCount = currentFileCount
    qt.QTimer.singleShot(500, self.startTimer)

  def createCurrentFileList(self, directory):
    self.currentFileList = []
    for item in self.getFileList(directory):
      self.currentFileList.append(item)

    if len(self.currentFileList) > 1:
      self.thereAreFilesInTheFolderFlag = 1
      self.importDICOMSeries(len(self.currentFileList))
    else:
      self.thereAreFilesInTheFolderFlag = 0

  def createLoadableFileListFromSelection(self, selectedSeries):

    if os.path.exists(self._intraopDataDir):
      self.loadableList = dict()
      self.loadableList[selectedSeries] = []

      for dcm in self.getFileList(self._intraopDataDir):
        currentFile = os.path.join(self._intraopDataDir, dcm)
        seriesNumberDescription = self.makeSeriesNumberDescription(currentFile)
        if seriesNumberDescription and seriesNumberDescription in selectedSeries:
          self.loadableList[selectedSeries].append(currentFile)

  def loadSeriesIntoSlicer(self, selectedSeries, clearOldSeries=False):
    if clearOldSeries:
      self.clearAlreadyLoadedSeries()
    self.createLoadableFileListFromSelection(selectedSeries)

    if selectedSeries not in self.alreadyLoadedSeries.keys():
      files = self.loadableList[selectedSeries]
      scalarVolumePlugin = slicer.modules.dicomPlugins['DICOMScalarVolumePlugin']()
      loadables = scalarVolumePlugin.examine([files])
      volume = scalarVolumePlugin.load(loadables[0])
      volume.SetName(loadables[0].name)
      slicer.mrmlScene.AddNode(volume)
      self.alreadyLoadedSeries[selectedSeries] = volume
    else:
      volume = self.alreadyLoadedSeries[selectedSeries]
    return volume

  def importDICOMSeries(self, currentFileCount):
    indexer = ctk.ctkDICOMIndexer()
    db = slicer.dicomDatabase

    if self.thereAreFilesInTheFolderFlag == 1:
      newFileList = self.currentFileList
      self.thereAreFilesInTheFolderFlag = 0
    else:
      newFileList = list(set(self.getFileList(self._intraopDataDir)) - set(self.currentFileList))

    for currentFile in newFileList:
      currentFile = os.path.join(self._intraopDataDir, currentFile)
      indexer.addFile(db, currentFile, None)
      seriesNumberDescription = self.makeSeriesNumberDescription(currentFile)
      if seriesNumberDescription and seriesNumberDescription not in self.seriesList:
        self.seriesList.append(seriesNumberDescription)

    indexer.addDirectory(db, self._intraopDataDir)
    indexer.waitForImportFinished()

    self.seriesList = sorted(self.seriesList, key=lambda series: RegistrationResult.getSeriesNumberFromString(series))

    if self._incomingDataCallback and len(newFileList) > 0 and \
                    len(self.getFileList(self._intraopDataDir)) == currentFileCount:
      self._incomingDataCallback(newList=newFileList)

  def makeSeriesNumberDescription(self, dicomFile):
    seriesDescription = self.getDICOMValue(dicomFile, DICOMTAGS.SERIES_DESCRIPTION)
    seriesNumber = self.getDICOMValue(dicomFile, DICOMTAGS.SERIES_NUMBER)
    seriesNumberDescription = None
    if seriesDescription and seriesNumber:
      seriesNumberDescription = seriesNumber + ": " + seriesDescription
    return seriesNumberDescription

  def getTargetPositions(self, registeredTargets):
    number_of_targets = registeredTargets.GetNumberOfFiducials()
    target_positions = []
    for target in range(number_of_targets):
      target_position = [0.0, 0.0, 0.0]
      registeredTargets.GetNthFiducialPosition(target, target_position)
      target_positions.append(target_position)
    logging.debug('target_positions are ' + str(target_positions))
    return target_positions

  def getNeedleTipTargetDistance2D(self, target_position, needleTip_position):
    x = abs(target_position[0] - needleTip_position[0])
    y = abs(target_position[1] - needleTip_position[1])
    return [x, y]

  def getNeedleTipTargetDistance3D(self, target_position, needleTip_position):
    rulerNode = slicer.vtkMRMLAnnotationRulerNode()
    rulerNode.SetPosition1(target_position)
    rulerNode.SetPosition2(needleTip_position)
    distance_3D = rulerNode.GetDistanceMeasurement()
    return distance_3D

  def setupColorTable(self, colorFile):

    self.mpReviewColorNode = slicer.vtkMRMLColorTableNode()
    colorNode = self.mpReviewColorNode
    colorNode.SetName('mpReview')
    slicer.mrmlScene.AddNode(colorNode)
    colorNode.SetTypeToUser()
    with open(colorFile) as f:
      n = sum(1 for line in f)
    colorNode.SetNumberOfColors(n - 1)
    import csv
    self.structureNames = []
    with open(colorFile, 'rb') as csvfile:
      reader = csv.DictReader(csvfile, delimiter=',')
      for index, row in enumerate(reader):
        colorNode.SetColor(index, row['Label'], float(row['R']) / 255,
                           float(row['G']) / 255, float(row['B']) / 255, float(row['A']))
        self.structureNames.append(row['Label'])

  def run(self):
    return True

  def runQuickSegmentationMode(self):
    self.setVolumeClipUserMode()
    self.placeFiducials()

  def setVolumeClipUserMode(self):
    lm = slicer.app.layoutManager()
    for widgetName in ['Red', 'Green', 'Yellow']:
      slice = lm.sliceWidget(widgetName)
      sliceLogic = slice.sliceLogic()
      sliceLogic.FitSliceToAll()

    # set the mouse mode into Markups fiducial placement
    placeModePersistence = 1
    slicer.modules.markups.logic().StartPlaceMode(placeModePersistence)

  def updateModel(self, observer, caller):
    import VolumeClipWithModel
    clipLogic = VolumeClipWithModel.VolumeClipWithModelLogic()
    clipLogic.updateModelFromMarkup(self.inputMarkupNode, self.clippingModelNode)

  def deleteClippingData(self):
    slicer.mrmlScene.RemoveNode(self.clippingModelNode)
    logging.debug('deleted ModelNode')
    slicer.mrmlScene.RemoveNode(self.inputMarkupNode)
    logging.debug('deleted inputMarkupNode')

  def placeFiducials(self):
    self.clippingModelNode = slicer.vtkMRMLModelNode()
    self.clippingModelNode.SetName('clipModelNode')
    slicer.mrmlScene.AddNode(self.clippingModelNode)

    self.createClippingModelDisplayNode()
    self.createMarkupAndDisplayNodeForFiducials()
    self.inputMarkupNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.updateModel)

  def createMarkupAndDisplayNodeForFiducials(self):
    self.displayNode = slicer.vtkMRMLMarkupsDisplayNode()
    slicer.mrmlScene.AddNode(self.displayNode)
    self.inputMarkupNode = slicer.vtkMRMLMarkupsFiducialNode()
    self.inputMarkupNode.SetName('inputMarkupNode')
    slicer.mrmlScene.AddNode(self.inputMarkupNode)
    self.inputMarkupNode.SetAndObserveDisplayNodeID(self.displayNode.GetID())
    self.styleDisplayNode(self.displayNode)

  def styleDisplayNode(self, displayNode):
    displayNode.SetTextScale(0)
    displayNode.SetGlyphScale(2.0)

  def createClippingModelDisplayNode(self):
    clippingModelDisplayNode = slicer.vtkMRMLModelDisplayNode()
    clippingModelDisplayNode.SetSliceIntersectionThickness(3)
    clippingModelDisplayNode.SetColor([0.200, 0.800, 0.000]) # green for glant
    clippingModelDisplayNode.BackfaceCullingOff()
    clippingModelDisplayNode.SliceIntersectionVisibilityOn()
    clippingModelDisplayNode.SetOpacity(0.3)
    slicer.mrmlScene.AddNode(clippingModelDisplayNode)
    self.clippingModelNode.SetAndObserveDisplayNodeID(clippingModelDisplayNode.GetID())

  def labelMapFromClippingModel(self, inputVolume):
    outputLabelMap = slicer.vtkMRMLLabelMapVolumeNode()
    slicer.mrmlScene.AddNode(outputLabelMap)

    params = {'sampleDistance': 0.1, 'labelValue': 8, 'InputVolume': inputVolume.GetID(),
              'surface': self.clippingModelNode.GetID(), 'OutputVolume': outputLabelMap.GetID()}

    logging.debug(params)
    slicer.cli.run(slicer.modules.modeltolabelmap, None, params, wait_for_completion=True)

    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeRed").SetUseLabelOutline(True)
    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeYellow").SetUseLabelOutline(True)

    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeRed").RotateToVolumePlane(outputLabelMap)
    slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeYellow").RotateToVolumePlane(outputLabelMap)

    return outputLabelMap

  def runBRAINSResample(self, inputVolume, referenceVolume, outputVolume, warpTransform):

    params = {'inputVolume': inputVolume, 'referenceVolume': referenceVolume, 'outputVolume': outputVolume,
              'warpTransform': warpTransform, 'interpolationMode': 'NearestNeighbor'}

    logging.debug('about to run BRAINSResample CLI with those params: ')
    logging.debug(params)
    slicer.cli.run(slicer.modules.brainsresample, None, params, wait_for_completion=True)
    logging.debug('resample labelmap through')
    slicer.mrmlScene.AddNode(outputVolume)

  def loadZFrameModel(self):
    zFrameModelPath = os.path.join(self.modulePath, self.ZFRAME_MODEL_PATH)
    self.zFrameModelNode = slicer.mrmlScene.GetNodeByID(self.zFrameModelNodeID)
    if not self.zFrameModelNode:
      _, self.zFrameModelNode = slicer.util.loadModel(zFrameModelPath, returnNode=True)
      self.zFrameModelNode.SetName('ZFrameModel')
      slicer.mrmlScene.AddNode(self.zFrameModelNode)
      self.zFrameModelNodeID = self.zFrameModelNode.GetID()
    self.zFrameModelNode.SetDisplayVisibility(False)

  def runZFrameRegistration(self, inputVolume):
    # TODO: create configfile that chooses the Registration algorithm to use
    configFilePath = os.path.join(self.modulePath, 'Resources/zframe', 'zframe-config.csv')
    registration = LineMarkerRegistration(inputVolume, configFilePath)
    registration.runRegistration()
    self.zFrameTransform = registration.getOutputTransformation()
    slicer.mrmlScene.AddNode(registration.getOutputVolume())
    self.applyZFrameTransform(self.zFrameTransform)

  def loadTemplateConfigFile(self, path):
    self.templateIndex = []
    self.templateConfig = []

    reader = csv.reader(open(path, 'rb'))
    try:
      next(reader)
      for row in reader:
        self.templateIndex.append(row[0:2])
        self.templateConfig.append([float(row[2]), float(row[3]), float(row[4]),
                                    float(row[5]), float(row[6]), float(row[7]),
                                    float(row[8])])
    except csv.Error as e:
      print('file %s, line %d: %s' % (path, reader.line_num, e))
      return False

    self.createTemplateAndNeedlePathModel()
    self.setTemplateVisibility(0)
    self.setTemplatePathVisibility(0)
    self.setNeedlePathVisibility(0)
    self.updateTemplateVectors()
    return True

  def createTemplateAndNeedlePathModel(self):

    self.templatePathVectors = []
    self.templatePathOrigins = []

    self.checkAndCreateTemplateModelNode()
    self.checkAndCreatePathModelNode()

    pathModelAppend = vtk.vtkAppendPolyData()
    templateModelAppend = vtk.vtkAppendPolyData()

    for row in self.templateConfig:
      p, n = self.extractPointsAndNormalVectors(row)

      tempTubeFilter = self.createTubeFilter(p[0], p[1], radius=1.0, numSides=18)
      templateModelAppend.AddInputData(tempTubeFilter.GetOutput())
      templateModelAppend.Update()

      pathTubeFilter = self.createTubeFilter(p[0], p[2], radius=0.8, numSides=18)
      pathModelAppend.AddInputData(pathTubeFilter.GetOutput())
      pathModelAppend.Update()

      self.templatePathOrigins.append([row[0], row[1], row[2], 1.0])
      self.templatePathVectors.append([n[0], n[1], n[2], 1.0])
      self.templateMaxDepth.append(row[6])

    self.tempModelNode.SetAndObservePolyData(templateModelAppend.GetOutput())
    self.pathModelNode.SetAndObservePolyData(pathModelAppend.GetOutput())

  def createNeedleModelNode(self, start, end):
    self.removeNeedleModelNode()
    self.needleModelNode = self.createModelNode("ComputedNeedlePosition")
    modelDisplayNode = self.setAndObserveDisplayNode(self.needleModelNode)
    modelDisplayNode.SetColor(0, 1, 0)
    pathTubeFilter = self.createTubeFilter(start, end, radius=1.0, numSides=18)
    self.needleModelNode.SetAndObservePolyData(pathTubeFilter.GetOutput())

  def removeNeedleModelNode(self):
    if self.needleModelNode:
      slicer.mrmlScene.RemoveNode(self.needleModelNode)

  def extractPointsAndNormalVectors(self, row):
    p1 = numpy.array(row[0:3])
    p2 = numpy.array(row[3:6])
    v = p2-p1
    nl = numpy.linalg.norm(v)
    n = v/nl  # normal vector
    l = row[6]
    p3 = p1 + l * n
    return [p1, p2, p3], n

  def createTubeFilter(self, start, end, radius, numSides):
    lineSource = vtk.vtkLineSource()
    lineSource.SetPoint1(start)
    lineSource.SetPoint2(end)
    tubeFilter = vtk.vtkTubeFilter()

    tubeFilter.SetInputConnection(lineSource.GetOutputPort())
    tubeFilter.SetRadius(radius)
    tubeFilter.SetNumberOfSides(numSides)
    tubeFilter.CappingOn()
    tubeFilter.Update()
    return tubeFilter

  def checkAndCreatePathModelNode(self):
    if self.pathModelNode is None:
      self.pathModelNode = self.createModelNode('NeedleGuideNeedlePath')
      self.setAndObserveDisplayNode(self.pathModelNode)

  def checkAndCreateTemplateModelNode(self):
    if self.tempModelNode is None:
      self.tempModelNode = self.createModelNode('NeedleGuideTemplate')
      self.setAndObserveDisplayNode(self.tempModelNode)
      self.modelNodeTag = self.tempModelNode.AddObserver(slicer.vtkMRMLTransformableNode.TransformModifiedEvent,
                                                         self.updateTemplateVectors)

  def applyZFrameTransform(self, transform):
    for node in [node for node in [self.pathModelNode, self.tempModelNode, self.zFrameModelNode, self.needleModelNode] if node]:
      node.SetAndObserveTransformNodeID(transform.GetID())

  def setModelVisibility(self, node, visible):
    dnode = node.GetDisplayNode()
    if dnode is not None:
      dnode.SetVisibility(visible)

  def setModelSliceIntersectionVisibility(self, node, visible):
    dnode = node.GetDisplayNode()
    if dnode is not None:
      dnode.SetSliceIntersectionVisibility(visible)

  def setTemplateVisibility(self, visibility):
    self.setModelVisibility(self.tempModelNode, visibility)

  def setTemplatePathVisibility(self, visibility):
    self.showTemplatePath = visibility
    self.setModelVisibility(self.pathModelNode, visibility)
    self.setModelSliceIntersectionVisibility(self.pathModelNode, visibility)

  def setNeedlePathVisibility(self, visibility):
    self.showNeedlePath = visibility
    if self.needleModelNode:
      self.setModelVisibility(self.needleModelNode, visibility)
      self.setModelSliceIntersectionVisibility(self.needleModelNode, visibility)

  def updateTemplateVectors(self, observee=None, event=None):
    if self.tempModelNode is None:
      return

    trans = vtk.vtkMatrix4x4()
    transformNode = self.tempModelNode.GetParentTransformNode()
    if transformNode is not None:
      transformNode.GetMatrixTransformToWorld(trans)
    else:
      trans.Identity()

    # Calculate offset
    zero = [0.0, 0.0, 0.0, 1.0]
    offset = trans.MultiplyDoublePoint(zero)

    self.pathOrigins = []
    self.pathVectors = []

    for i, orig in enumerate(self.templatePathOrigins):
      torig = trans.MultiplyDoublePoint(orig)
      self.pathOrigins.append(numpy.array(torig[0:3]))
      vec = self.templatePathVectors[i]
      tvec = trans.MultiplyDoublePoint(vec)
      self.pathVectors.append(numpy.array([tvec[0]-offset[0], tvec[1]-offset[1], tvec[2]-offset[2]]))
      i += 1

  def computeNearestPath(self, pos):
    minMag2 = numpy.Inf
    minDepth = 0.0
    minIndex = -1

    p = numpy.array(pos)
    for i, orig in enumerate(self.pathOrigins):
      vec = self.pathVectors[i]
      op = p - orig
      aproj = numpy.inner(op, vec)
      perp = op-aproj*vec
      mag2 = numpy.vdot(perp, perp)
      if mag2 < minMag2:
        minMag2 = mag2
        minIndex = i
        minDepth = aproj
      i += 1

    indexX = '--'
    indexY = '--'
    inRange = False

    if minIndex != -1:
      indexX = self.templateIndex[minIndex][0]
      indexY = self.templateIndex[minIndex][1]
      if 0 < minDepth < self.templateMaxDepth[minIndex]:
        inRange = True
        p1 = self.pathOrigins[minIndex]
        v = self.pathVectors[minIndex]
        nl = numpy.linalg.norm(v)
        n = v/nl  # normal vector
        l = self.templateMaxDepth[minIndex]
        p2 = p1 + l * n
        self.createNeedleModelNode(p1, p2)
      else:
        self.removeNeedleModelNode()
      self.setTemplatePathVisibility(self.showTemplatePath)
      self.setNeedlePathVisibility(self.showNeedlePath)

    return indexX, indexY, minDepth, inRange

class SliceTrackerTest(ScriptedLoadableModuleTest):
  """
  This is the test case for your scripted module.
  Uses ScriptedLoadableModuleTest base class, available at:
  https://github.com/Slicer/Slicer/blob/master/Base/Python/slicer/ScriptedLoadableModule.py
  """

  def setUp(self):
    """ Do whatever is needed to reset the state - typically a scene clear will be enough.
    """
    slicer.mrmlScene.Clear(0)

  def runTest(self):
    """Run as few or as many tests as needed here.
    """
    self.setUp()
    self.test_SliceTracker1()

  def test_SliceTracker1(self):
    """ Ideally you should have several levels of tests.  At the lowest level
    tests should exercise the functionality of the logic with different inputs
    (both valid and invalid).  At higher levels your tests should emulate the
    way the user would interact with your code and confirm that it still works
    the way you intended.
    One of the most important features of the tests is that it should alert other
    developers when their changes will have an impact on the behavior of your
    module.  For example, if a developer removes a feature that you depend on,
    your test should break so they know that the feature is needed.
    """
    logging.debug(' ___ performing selfTest ___ ')


from collections import OrderedDict
from SliceTrackerUtils.decorators import onExceptReturnNone


class RegistrationResults(object):

  @property
  def activeResult(self):
    return self._getActiveResult()

  @activeResult.setter
  def activeResult(self, series):
    assert series in self._registrationResults.keys()
    self._activeResult = series

  @property
  @onExceptReturnNone
  def originalTargets(self):
    return self.getMostRecentApprovedCoverProstateRegistration().originalTargets

  @property
  @onExceptReturnNone
  def intraopLabel(self):
    return self.getMostRecentApprovedCoverProstateRegistration().fixedLabel

  @property
  @onExceptReturnNone
  def biasCorrectedResult(self):
    return self.getMostRecentApprovedCoverProstateRegistration().movingVolume

  def __init__(self):
    self._registrationResults = OrderedDict()
    self._activeResult = None
    self.preopTargets = None

  def _registrationResultHasStatus(self, series, status):
    if not type(series) is int:
      series = RegistrationResult.getSeriesNumberFromString(series)
    results = self.getResultsBySeriesNumber(series)
    return any(getattr(result, status) is True for result in results) if len(results) else False

  def registrationResultWasApproved(self, series):
    return self._registrationResultHasStatus(series, "approved")

  def registrationResultWasSkipped(self, series):
    return self._registrationResultHasStatus(series, "skipped")

  def registrationResultWasRejected(self, series):
    return self._registrationResultHasStatus(series, "rejected")

  def getResultsAsList(self):
    return self._registrationResults.values()

  def getMostRecentApprovedCoverProstateRegistration(self):
    mostRecent = None
    for result in self._registrationResults.values():
      if SliceTrackerConstants.COVER_PROSTATE in result.name and result.approved and not result.skipped:
        mostRecent = result
    return mostRecent

  def getLastApprovedRigidTransformation(self):
    nApprovedRegistrations = sum([1 for result in self._registrationResults.values() if result.approved])
    if nApprovedRegistrations == 1:
      logging.debug('Resampling label with same mask')
      # last registration was preop-intraop, take the same mask
      # this is an identity transform:
      lastRigidTfm = vtk.vtkGeneralTransform()
      lastRigidTfm.Identity()
    else:
      try:
        lastRigidTfm = self.getMostRecentApprovedResult().rigidTransform
      except AttributeError:
        lastRigidTfm = None
    return lastRigidTfm

  def getOrCreateResult(self, series):
    result = self.getResult(series)
    return result if result is not None else self.createResult(series)

  def createResult(self, series):
    assert series not in self._registrationResults.keys()
    self._registrationResults[series] = RegistrationResult(series)
    self.activeResult = series
    return self._registrationResults[series]

  @onExceptReturnNone
  def getResult(self, series):
    return self._registrationResults[series]

  def getResultsBySeries(self, series):
    seriesNumber = RegistrationResult.getSeriesNumberFromString(series)
    return self.getResultsBySeriesNumber(seriesNumber)

  def getResultsBySeriesNumber(self, seriesNumber):
    return [result for result in self.getResultsAsList() if seriesNumber == result.seriesNumber]

  def removeResult(self, series):
    try:
      del self._registrationResults[series]
    except KeyError:
      pass

  def exists(self, series):
    return series in self._registrationResults.keys()

  @onExceptReturnNone
  def _getActiveResult(self):
    return self._registrationResults[self._activeResult]

  @onExceptReturnNone
  def getMostRecentResult(self):
    lastKey = self._registrationResults.keys()[-1]
    return self._registrationResults[lastKey]

  @onExceptReturnNone
  def getMostRecentApprovedResult(self):
    for result in reversed(self._registrationResults.values()):
      if result.approved:
        return result
    return None

  @onExceptReturnNone
  def getMostRecentVolumes(self):
    return self.getMostRecentResult().volumes

  @onExceptReturnNone
  def getMostRecentTransforms(self):
    return self.getMostRecentResult().transforms

  @onExceptReturnNone
  def getMostRecentTargets(self):
    return self.getMostRecentResult().targets

  # def deleteRegistrationResult(self, series):
  #   result = self._registrationResults[series]
  #   # TODO: deleting targets causes total crash of Slicer
  #   nodesToDelete = ['fixedLabel', 'movingLabel', 'outputVolumeRigid', 'outputVolumeAffine', 'outputVolumeBSpline',
  #                    'outputTransformRigid', 'outputTransformAffine', 'outputTransformBSpline']
  #   # 'outputTargetsRigid', 'outputTargetAffine', 'outputTargetsBSpline']
  #   for node in [result[key] for key in nodesToDelete if key in result.keys()]:
  #     if node:
  #       slicer.mrmlScene.RemoveNodeReferences(node)
  #       slicer.mrmlScene.RemoveNode(node)
  #   del self._registrationResults[series]


class RegistrationResult(object):

  REGISTRATION_TYPE_NAMES = ['rigid', 'affine', 'bSpline']

  @staticmethod
  def getSeriesNumberFromString(text):
    return int(text.split(": ")[0])

  @property
  def volumes(self):
    return {'rigid': self.rigidVolume, 'affine': self.affineVolume, 'bSpline': self.bSplineVolume}

  @property
  def transforms(self):
    return {'rigid': self.rigidTransform, 'affine': self.affineTransform, 'bSpline': self.bSplineTransform}

  @property
  def targets(self):
    return {'rigid': self.rigidTargets, 'affine': self.affineTargets, 'bSpline': self.bSplineTargets}

  @property
  def name(self):
    return self._name

  @name.setter
  def name(self, name):
    splitted = name.split(': ')
    assert len(splitted) == 2
    self._name = name
    self._seriesNumber = int(splitted[0])
    self._seriesDescription = splitted[1]

  @property
  def seriesNumber(self):
    return self._seriesNumber

  @property
  def seriesDescription(self):
    return self._seriesDescription

  def __init__(self, series):
    self.name = series
    self.approved = False
    self.skipped = False
    self.rejected = False
    self.rigidVolume = None
    self.affineVolume = None
    self.bSplineVolume = None
    self.rigidTransform = None
    self.affineTransform = None
    self.bSplineTransform = None
    self.rigidTargets = None
    self.affineTargets = None
    self.bSplineTargets = None
    self.originalTargets = None

    self.movingVolume = None
    self.movingLabel = None
    self.fixedVolume = None
    self.fixedLabel = None

    self.tipPosition = None

    self.cmdArguments = ""

    self.score = None

  def __del__(self):
    # TODO: should it also delete the volumes etc.?
    pass

  def setVolume(self, regType, volume):
    self._setRegAttribute(regType, "Volume", volume)

  def getVolume(self, regType):
    return self._getRegAttribute(regType, "Volume")

  def setTransform(self, regType, transform):
    self._setRegAttribute(regType, "Transform", transform)

  def getTransform(self, regType):
    return self._getRegAttribute(regType, "Transform")

  def setTargets(self, regType, targets):
    self._setRegAttribute(regType, "Targets", targets)

  def getTargets(self, regType):
    return self._getRegAttribute(regType, "Targets")

  def _setRegAttribute(self, regType, attributeType, value):
    assert regType in self.REGISTRATION_TYPE_NAMES
    setattr(self, regType+attributeType, value)

  def _getRegAttribute(self, regType, attributeType):
    assert regType in self.REGISTRATION_TYPE_NAMES
    return getattr(self, regType+attributeType)

  def approve(self):
    self.approved = True
    self.skipped = False
    self.rejected = False

  def skip(self):
    self.skipped = True
    self.approved = False
    self.rejected = False

  def reject(self):
    self.rejected = True
    self.skipped = False
    self.approved = False

  def printSummary(self):
    logging.debug('# ___________________________  registration output  ________________________________')
    logging.debug(self.__dict__)
    logging.debug('# __________________________________________________________________________________')


class IncomingDataMessageBox(ExtendedQMessageBox):

  def __init__(self, parent=None):
    super(IncomingDataMessageBox, self).__init__(parent)
    self.setWindowTitle("Dialog with CheckBox")
    self.setText("New data has been received. What would you do?")
    self.setIcon(qt.QMessageBox.Question)
    trackButton =  self.addButton(qt.QPushButton('Track targets'), qt.QMessageBox.AcceptRole)
    self.addButton(qt.QPushButton('Postpone'), qt.QMessageBox.NoRole)
    self.setDefaultButton(trackButton)


class RatingWindow(qt.QWidget, ModuleWidgetMixin):

  @property
  def maximumValue(self):
    return self._maximumValue

  @maximumValue.setter
  def maximumValue(self, value):
    if value < 1:
      raise ValueError("The maximum rating value cannot be less than 1.")
    else:
      self._maximumValue = value

  def __init__(self, maximumValue, text="Please rate the registration result:", *args):
    qt.QWidget.__init__(self, *args)
    self.maximumValue = maximumValue
    self.text = text
    self.iconPath = os.path.join(os.path.dirname(sys.modules[self.__module__].__file__), 'Resources/Icons')
    self.setupIcons()
    self.setLayout(qt.QGridLayout())
    self.setWindowFlags(qt.Qt.WindowStaysOnTopHint | qt.Qt.FramelessWindowHint)
    self.setupElements()
    self.connectButtons()
    self.showRatingValue = True

  def __del__(self):
    self.disconnectButtons()

  def isRatingEnabled(self):
    return not self.disableWidgetCheckbox.checked

  def setupIcons(self):
    self.filledStarIcon = self.createIcon("icon-star-filled.png", self.iconPath)
    self.unfilledStarIcon = self.createIcon("icon-star-unfilled.png", self.iconPath)

  def show(self, disableWidget=None, callback=None):
    self.disabledWidget = disableWidget
    if disableWidget:
      disableWidget.enabled = False
    qt.QWidget.show(self)
    self.ratingScore = None
    self.callback = callback

  def setupElements(self):
    self.layout().addWidget(qt.QLabel(self.text), 0, 0)
    self.ratingButtonGroup = qt.QButtonGroup()
    for rateValue in range(1, self.maximumValue+1):
      attributeName = "button"+str(rateValue)
      setattr(self, attributeName, self.createButton('', icon=self.unfilledStarIcon))
      self.ratingButtonGroup.addButton(getattr(self, attributeName), rateValue)

    for button in list(self.ratingButtonGroup.buttons()):
      button.setCursor(qt.Qt.PointingHandCursor)

    self.ratingLabel = self.createLabel("")
    row = self.createHLayout(list(self.ratingButtonGroup.buttons()) + [self.ratingLabel])
    self.layout().addWidget(row, 1, 0)

    self.disableWidgetCheckbox = qt.QCheckBox("Don't display this window again")
    self.disableWidgetCheckbox.checked = False
    self.layout().addWidget(self.disableWidgetCheckbox, 2, 0)

  def connectButtons(self):
    self.ratingButtonGroup.connect('buttonClicked(int)', self.onRatingButtonClicked)
    for button in list(self.ratingButtonGroup.buttons()):
      button.installEventFilter(self)

  def disconnectButtons(self):
    self.ratingButtonGroup.disconnect('buttonClicked(int)', self.onRatingButtonClicked)
    for button in list(self.ratingButtonGroup.buttons()):
      button.removeEventFilter(self)

  def eventFilter(self, obj, event):
    if obj in list(self.ratingButtonGroup.buttons()) and event.type() == qt.QEvent.HoverEnter:
      self.onHoverEvent(obj)
    elif obj in list(self.ratingButtonGroup.buttons()) and event.type() == qt.QEvent.HoverLeave:
      self.onLeaveEvent()
    return qt.QWidget.eventFilter(self, obj, event)

  def onLeaveEvent(self):
    for button in list(self.ratingButtonGroup.buttons()):
      button.icon = self.unfilledStarIcon

  def onHoverEvent(self, obj):
    ratingValue = 0
    for button in list(self.ratingButtonGroup.buttons()):
      button.icon = self.filledStarIcon
      ratingValue += 1
      if obj is button:
        break
    if self.showRatingValue:
      self.ratingLabel.setText(str(ratingValue))

  def onRatingButtonClicked(self, buttonId):
    self.ratingScore = buttonId
    if self.disabledWidget:
      self.disabledWidget.enabled = True
      self.disabledWidget = None
    if self.callback:
      self.callback(self.ratingScore)
    self.hide()


class CustomTargetTableModel(qt.QAbstractTableModel):

  @property
  def targetList(self):
    return self._targetList

  @targetList.setter
  def targetList(self, targetList):
    self.reset()
    self._targetList = targetList
    self.dataChanged(self.index(0, 3), self.index(self.rowCount(None)-1, 4))

  @property
  def cursorPosition(self):
    return self._cursorPosition

  @cursorPosition.setter
  def cursorPosition(self, cursorPosition):
    self._cursorPosition = cursorPosition
    self.dataChanged(self.index(0, 1), self.index(self.rowCount(None)-1, 2))

  def __init__(self, logic, targets=None, parent=None, *args):
    qt.QAbstractTableModel.__init__(self, parent, *args)
    self.logic = logic
    self._cursorPosition = None
    self._targetList = None
    self.targetList = targets
    self.headers = ['Name', 'Needle-tip distance 2D [mm]', 'Needle-tip distance 3D [mm]', 'Hole', 'Depth [mm]']

  def headerData(self, col, orientation, role):
    if orientation == qt.Qt.Horizontal and role == qt.Qt.DisplayRole:
        return self.headers[col]
    return None

  def rowCount(self, parent):
    try:
      number_of_targets = self.targetList.GetNumberOfFiducials()
      return number_of_targets
    except AttributeError:
      return 0

  def columnCount(self, parent):
    return len(self.headers)

  def data(self, index, role):
    if not index.isValid():
      return None
    elif role != qt.Qt.DisplayRole:
      return None

    col = index.column()

    targetPosition = [0.0, 0.0, 0.0]
    if col in [1,2,3,4]:
      self.targetList.GetNthFiducialPosition(index.row(), targetPosition)

    if col == 0:
      return self.targetList.GetNthFiducialLabel(index.row())
    elif col == 1 and self.cursorPosition:
      distance2D = self.logic.getNeedleTipTargetDistance2D(targetPosition, self.cursorPosition)
      return 'x = ' + str(round(distance2D[0], 2)) + ' y = ' + str(round(distance2D[1], 2))
    elif col == 2 and self.cursorPosition:
      distance3D = self.logic.getNeedleTipTargetDistance3D(targetPosition, self.cursorPosition)
      return str(round(distance3D, 2))
    elif (col == 3 or col == 4) and self.logic.zFrameRegistrationSuccessful:
      (indexX, indexY, depth, inRange) = self.logic.computeNearestPath(targetPosition)
      if col == 3:
        return '(%s, %s)' % (indexX, indexY)
      if col == 4:
        return '%.3f' % depth if inRange else '(%.3f)' % depth
    return ""