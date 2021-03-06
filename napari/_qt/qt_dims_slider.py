from typing import Optional, Tuple

import numpy as np
from qtpy.QtCore import QObject, Qt, QTimer, Signal, Slot
from qtpy.QtGui import QIntValidator
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
    QFrame,
)

from ..components.dims_constants import DimsMode
from ..utils.event import Event
from ._constants import LoopMode
from .qt_modal import QtPopup
from .qt_scrollbar import ModifiedScrollBar
from .utils import new_worker_qthread


class QtDimSliderWidget(QWidget):
    """Compound widget to hold the label, slider and play button for an axis.

    These will usually be instantiated in the QtDims._create_sliders method.
    This widget *must* be instantiated with a parent QtDims.
    """

    axis_label_changed = Signal(int, str)  # axis, label
    fps_changed = Signal(float)
    mode_changed = Signal(str)
    range_changed = Signal(tuple)
    play_started = Signal()
    play_stopped = Signal()

    def __init__(self, parent: QWidget, axis: int):
        super().__init__(parent=parent)
        self.axis = axis
        self.qt_dims = parent
        self.dims = parent.dims
        self.axis_label = None
        self.slider = None
        self.play_button = None
        self.curslice_label = QLineEdit(self)
        self.curslice_label.setToolTip(f'Current slice for axis {axis}')
        # if we set the QIntValidator to actually reflect the range of the data
        # then an invalid (i.e. too large) index doesn't actually trigger the
        # editingFinished event (the user is expected to change the value)...
        # which is confusing to the user, so instead we use an IntValidator
        # that makes sure the user can only enter integers, but we do our own
        # value validation in change_slice
        self.curslice_label.setValidator(QIntValidator(0, 999999))

        def change_slice():
            val = int(self.curslice_label.text())
            max_allowed = self.dims.max_indices[self.axis]
            if val > max_allowed:
                val = max_allowed
                self.curslice_label.setText(str(val))
            self.curslice_label.clearFocus()
            self.qt_dims.setFocus()
            self.dims.set_point(self.axis, val)

        self.curslice_label.editingFinished.connect(change_slice)
        self.totslice_label = QLabel(self)
        self.totslice_label.setToolTip(f'Total slices for axis {axis}')
        self.curslice_label.setObjectName('slice_label')
        self.totslice_label.setObjectName('slice_label')
        sep = QFrame(self)
        sep.setFixedSize(1, 14)
        sep.setObjectName('slice_label_sep')

        self._fps = 10
        self._minframe = None
        self._maxframe = None
        self._loop_mode = LoopMode.LOOP

        layout = QHBoxLayout()
        self._create_axis_label_widget()
        self._create_range_slider_widget()
        self._create_play_button_widget()

        layout.addWidget(self.axis_label)
        layout.addWidget(self.play_button)
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.curslice_label)
        layout.addWidget(sep)
        layout.addWidget(self.totslice_label)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.setLayout(layout)
        self.dims.events.axis_labels.connect(self._pull_label)

    def _create_axis_label_widget(self):
        """Create the axis label widget which accompanies its slider."""
        label = QLineEdit(self)
        label.setObjectName('axis_label')  # needed for _update_label
        label.setText(self.dims.axis_labels[self.axis])
        label.home(False)
        label.setToolTip('Edit to change axis label')
        label.setAcceptDrops(False)
        label.setEnabled(True)
        label.setAlignment(Qt.AlignRight)
        label.setContentsMargins(0, 0, 2, 0)
        label.textChanged.connect(self._update_label)
        label.editingFinished.connect(self._clear_label_focus)
        self.axis_label = label

    def _create_range_slider_widget(self):
        """Creates a range slider widget for a given axis."""
        _range = self.dims.range[self.axis]
        # Set the maximum values of the range slider to be one step less than
        # the range of the layer as otherwise the slider can move beyond the
        # shape of the layer as the endpoint is included
        _range = (_range[0], _range[1] - _range[2], _range[2])
        point = self.dims.point[self.axis]

        slider = ModifiedScrollBar(Qt.Horizontal)
        slider.setFocusPolicy(Qt.NoFocus)
        slider.setMinimum(_range[0])
        slider.setMaximum(_range[1])
        slider.setSingleStep(_range[2])
        slider.setPageStep(_range[2])
        slider.setValue(point)

        # Listener to be used for sending events back to model:
        slider.valueChanged.connect(
            lambda value: self.dims.set_point(self.axis, value)
        )

        def slider_focused_listener():
            self.qt_dims.last_used = self.axis

        # linking focus listener to the last used:
        slider.sliderPressed.connect(slider_focused_listener)
        self.slider = slider

    def _create_play_button_widget(self):
        """Creates the actual play button, which has the modal popup."""
        self.play_button = QtPlayButton(self.qt_dims, self.axis)
        self.play_button.mode_combo.activated[str].connect(
            lambda x: self.__class__.loop_mode.fset(
                self, LoopMode(x.replace(' ', '_'))
            )
        )

        def fps_listener(*args):
            fps = self.play_button.fpsspin.value()
            fps *= -1 if self.play_button.reverse_check.isChecked() else 1
            self.__class__.fps.fset(self, fps)

        self.play_button.fpsspin.editingFinished.connect(fps_listener)
        self.play_button.reverse_check.stateChanged.connect(fps_listener)
        self.play_stopped.connect(self.play_button._handle_stop)
        self.play_started.connect(self.play_button._handle_start)

    def _pull_label(self, event):
        """Updates the label LineEdit from the dims model."""
        if event.axis == self.axis:
            label = self.dims.axis_labels[self.axis]
            self.axis_label.setText(label)
            self.axis_label_changed.emit(self.axis, label)

    def _update_label(self):
        with self.dims.events.axis_labels.blocker():
            self.dims.set_axis_label(self.axis, self.axis_label.text())
        self.axis_label_changed.emit(self.axis, self.axis_label.text())

    def _clear_label_focus(self):
        self.axis_label.clearFocus()
        self.qt_dims.setFocus()

    def _update_range(self):
        """Updates range for slider."""
        displayed_sliders = self.qt_dims._displayed_sliders

        _range = self.dims.range[self.axis]
        _range = (_range[0], _range[1] - _range[2], _range[2])
        if _range not in (None, (None, None, None)):
            if _range[1] == 0:
                displayed_sliders[self.axis] = False
                self.qt_dims.last_used = None
                self.slider.hide()
            else:
                if (
                    not displayed_sliders[self.axis]
                    and self.axis not in self.dims.displayed
                ):
                    displayed_sliders[self.axis] = True
                    self.last_used = self.axis
                    self.slider.show()
                self.slider.setMinimum(_range[0])
                self.slider.setMaximum(_range[1])
                self.slider.setSingleStep(_range[2])
                self.slider.setPageStep(_range[2])
                maxi = self.dims.max_indices[self.axis]
                self.totslice_label.setText(str(int(maxi)))
                self.totslice_label.setAlignment(Qt.AlignLeft)
                self._update_slice_labels()
        else:
            displayed_sliders[self.axis] = False
            self.slider.hide()

    def _update_slider(self):
        mode = self.dims.mode[self.axis]
        if mode == DimsMode.POINT:
            self.slider.setValue(self.dims.point[self.axis])
            self._update_slice_labels()

    def _update_slice_labels(self):
        step = self.dims.range[self.axis][2]
        self.curslice_label.setText(
            str(int(self.dims.point[self.axis] // step))
        )
        self.curslice_label.setAlignment(Qt.AlignRight)

    @property
    def fps(self):
        return self._fps

    @fps.setter
    def fps(self, value):
        self._fps = value
        self.play_button.fpsspin.setValue(abs(value))
        self.play_button.reverse_check.setChecked(value < 0)
        self.fps_changed.emit(value)

    @property
    def loop_mode(self):
        return self._loop_mode

    @loop_mode.setter
    def loop_mode(self, value):
        self._loop_mode = value
        self.play_button.mode_combo.setCurrentText(str(value))
        self.mode_changed.emit(str(value))

    @property
    def frame_range(self):
        frame_range = (self._minframe, self._maxframe)
        frame_range = frame_range if any(frame_range) else None
        return frame_range

    @frame_range.setter
    def frame_range(self, value):
        if not isinstance(value, (tuple, list, type(None))):
            raise TypeError('frame_range value must be a list or tuple')
        if value and not len(value) == 2:
            raise ValueError('frame_range must have a length of 2')
        if value is None:
            value = (None, None)
        self._minframe, self._maxframe = value
        self.range_changed.emit(tuple(value))

    def _update_play_settings(self, fps, loop_mode, frame_range):
        if fps is not None:
            self.fps = fps
        if loop_mode is not None:
            self.loop_mode = loop_mode
        if frame_range is not None:
            self.frame_range = frame_range

    def _play(
        self,
        fps: Optional[float] = None,
        loop_mode: Optional[str] = None,
        frame_range: Optional[Tuple[int, int]] = None,
    ):
        """Animate (play) axis. Same API as QtDims.play()

        Putting the AnimationWorker logic here makes it easier to call
        QtDims.play(axis), or hit the keybinding, and have each axis remember
        it's own settings (fps, mode, etc...).
        """

        # having this here makes sure that using the QtDims.play() API
        # keeps the play preferences synchronized with the play_button.popup
        self._update_play_settings(fps, loop_mode, frame_range)

        # setting fps to 0 just stops the animation
        if fps == 0:
            return

        worker, thread = new_worker_qthread(
            AnimationWorker,
            self,
            start=True,
            connections={'frame_requested': self.qt_dims._set_frame},
        )
        worker.finished.connect(self.qt_dims.stop)
        thread.finished.connect(self.play_stopped.emit)
        self.play_started.emit()
        self.thread = thread
        return worker, thread


class QtCustomDoubleSpinBox(QDoubleSpinBox):
    """Custom Spinbox that emits an additional editingFinished signal whenever
    the valueChanged event is emitted AND the left mouse button is down.

    The original use case here was the FPS spinbox in the play button, where
    hooking to the actual valueChanged event is undesireable, because if the
    user clears the LineEdit to type, for example, "0.5", then play back
    will temporarily pause when "0" is typed (if the animation is currently
    running).  However, the editingFinished event ignores mouse click events on
    the spin buttons.  This subclass class triggers an event both during
    editingFinished and when the user clicks on the spin buttons.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, *kwargs)
        self.valueChanged.connect(self.custom_change_event)

    def custom_change_event(self, value):
        if QApplication.mouseButtons() & Qt.LeftButton:
            self.editingFinished.emit()

    def textFromValue(self, value):
        """This removes the decimal places if the float is an integer"""
        if value.is_integer():
            value = int(value)
        return str(value)

    def keyPressEvent(self, event):
        # this is here to intercept Return/Enter keys when editing the FPS
        # SpinBox.  We WANT the return key to close the popup normally,
        # but if the user is editing the FPS spinbox, we simply want to
        # register the change and lose focus on the lineEdit, in case they
        # want to make an additional change (without reopening the popup)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.editingFinished.emit()
            self.clearFocus()
            return
        super().keyPressEvent(event)


class QtPlayButton(QPushButton):
    """Play button, included in the DimSliderWidget, to control playback

    the button also owns the QtModalPopup that controls the playback settings.
    """

    play_requested = Signal(int)  # axis, fps

    def __init__(self, dims, axis, reverse=False, fps=10, mode=LoopMode.LOOP):
        super().__init__()
        self.dims = dims
        self.axis = axis
        self.reverse = reverse
        self.fps = fps
        self.mode = mode
        self.setProperty('reverse', str(reverse))  # for styling
        self.setProperty('playing', 'False')  # for styling

        # build popup modal form

        self.popup = QtPopup(self)
        form_layout = QFormLayout()
        self.popup.frame.setLayout(form_layout)

        fpsspin = QtCustomDoubleSpinBox(self.popup)
        fpsspin.setAlignment(Qt.AlignCenter)
        fpsspin.setValue(self.fps)
        if hasattr(fpsspin, 'setStepType'):
            # this was introduced in Qt 5.12.  Totally optional, just nice.
            fpsspin.setStepType(QDoubleSpinBox.AdaptiveDecimalStepType)
        fpsspin.setMaximum(500)
        fpsspin.setMinimum(0)
        form_layout.insertRow(
            0, QLabel('frames per second:', parent=self.popup), fpsspin
        )
        self.fpsspin = fpsspin

        revcheck = QCheckBox(self.popup)
        form_layout.insertRow(
            1, QLabel('play direction:', parent=self.popup), revcheck
        )
        self.reverse_check = revcheck

        # THIS IS HERE TEMPORARILY UNTIL I CAN ADD FRAME_RANGE TO THE POPUP
        # dimsrange = dims.dims.range[axis]
        # minspin = QDoubleSpinBox(self.popup)
        # minspin.setAlignment(Qt.AlignCenter)
        # minspin.setValue(dimsrange[0])
        # minspin.valueChanged.connect(self.set_minframe)
        # form_layout.insertRow(
        #     1, QLabel('start frame:', parent=self.popup), minspin
        # )

        # maxspin = QDoubleSpinBox(self.popup)
        # maxspin.setAlignment(Qt.AlignCenter)
        # maxspin.setValue(dimsrange[1] * dimsrange[2])
        # maxspin.valueChanged.connect(self.set_maxframe)
        # form_layout.insertRow(
        #     2, QLabel('end frame:', parent=self.popup), maxspin
        # )

        mode_combo = QComboBox(self.popup)
        mode_combo.addItems([str(i).replace('_', ' ') for i in LoopMode])
        form_layout.insertRow(
            2, QLabel('play mode:', parent=self.popup), mode_combo
        )
        mode_combo.setCurrentText(str(self.mode))
        self.mode_combo = mode_combo

    def mouseReleaseEvent(self, event):
        # using this instead of self.customContextMenuRequested.connect and
        # clicked.connect because the latter was not sending the
        # rightMouseButton release event.
        if event.button() == Qt.RightButton:
            self.popup.show_above_mouse()
        elif event.button() == Qt.LeftButton:
            self._on_click()

    def _on_click(self):
        if self.property('playing') == "True":
            return self.dims.stop()
        self.play_requested.emit(self.axis)

    def _handle_start(self):
        self.setProperty('playing', 'True')
        self.style().unpolish(self)
        self.style().polish(self)

    def _handle_stop(self):
        self.setProperty('playing', 'False')
        self.style().unpolish(self)
        self.style().polish(self)


class AnimationWorker(QObject):
    """A thread to keep the animation timer independent of the main event loop.

    This prevents mouseovers and other events from causing animation lag. See
    QtDims.play() for public-facing docstring.
    """

    frame_requested = Signal(int, int)  # axis, point
    finished = Signal()
    started = Signal()

    def __init__(self, slider):
        super().__init__()
        self.slider = slider
        self.dims = slider.dims
        self.axis = slider.axis
        self.loop_mode = slider.loop_mode
        slider.fps_changed.connect(self.set_fps)
        slider.mode_changed.connect(self.set_loop_mode)
        slider.range_changed.connect(self.set_frame_range)
        self.set_fps(self.slider.fps)
        self.set_frame_range(slider.frame_range)

        # after dims.set_point is called, it will emit a dims.events.axis()
        # we use this to update this threads current frame (in case it
        # was some other event that updated the axis)
        self.dims.events.axis.connect(self._on_axis_changed)
        self.current = max(self.dims.point[self.axis], self.min_point)
        self.current = min(self.current, self.max_point)
        self.timer = QTimer()

    @Slot()
    def work(self):
        # if loop_mode is once and we are already on the last frame,
        # return to the first frame... (so the user can keep hitting once)
        if self.loop_mode == LoopMode.ONCE:
            if self.step > 0 and self.current >= self.max_point - 1:
                self.frame_requested.emit(self.axis, self.min_point)
            elif self.step < 0 and self.current <= self.min_point + 1:
                self.frame_requested.emit(self.axis, self.max_point)
            self.timer.singleShot(self.interval, self.advance)
        else:
            # immediately advance one frame
            self.advance()
        self.started.emit()

    @Slot(float)
    def set_fps(self, fps):
        if fps == 0:
            return self.finish()
        self.step = 1 if fps > 0 else -1  # negative fps plays in reverse
        self.interval = 1000 / abs(fps)

    @Slot(tuple)
    def set_frame_range(self, frame_range):
        self.dimsrange = self.dims.range[self.axis]

        if frame_range is not None:
            if frame_range[0] >= frame_range[1]:
                raise ValueError("frame_range[0] must be <= frame_range[1]")
            if frame_range[0] < self.dimsrange[0]:
                raise IndexError("frame_range[0] out of range")
            if frame_range[1] * self.dimsrange[2] >= self.dimsrange[1]:
                raise IndexError("frame_range[1] out of range")
        self.frame_range = frame_range

        if self.frame_range is not None:
            self.min_point, self.max_point = self.frame_range
        else:
            self.min_point = 0
            self.max_point = int(
                np.floor(self.dimsrange[1] - self.dimsrange[2])
            )
        self.max_point += 1  # range is inclusive

    @Slot(str)
    def set_loop_mode(self, mode):
        self.loop_mode = LoopMode(mode)

    def advance(self):
        """Advance the current frame in the animation.

        Takes dims scale into account and restricts the animation to the
        requested frame_range, if entered.
        """
        self.current += self.step * self.dimsrange[2]
        if self.current < self.min_point:
            if (
                self.loop_mode == LoopMode.BACK_AND_FORTH
            ):  # 'loop_back_and_forth'
                self.step *= -1
                self.current = self.min_point + self.step * self.dimsrange[2]
            elif self.loop_mode == LoopMode.LOOP:  # 'loop'
                self.current = self.max_point + self.current - self.min_point
            else:  # loop_mode == 'once'
                return self.finish()
        elif self.current >= self.max_point:
            if (
                self.loop_mode == LoopMode.BACK_AND_FORTH
            ):  # 'loop_back_and_forth'
                self.step *= -1
                self.current = (
                    self.max_point + 2 * self.step * self.dimsrange[2]
                )
            elif self.loop_mode == LoopMode.LOOP:  # 'loop'
                self.current = self.min_point + self.current - self.max_point
            else:  # loop_mode == 'once'
                return self.finish()
        with self.dims.events.axis.blocker(self._on_axis_changed):
            self.frame_requested.emit(self.axis, self.current)
        # using a singleShot timer here instead of timer.start() because
        # it makes it easier to update the interval using signals/slots
        self.timer.singleShot(self.interval, self.advance)

    def finish(self):
        self.finished.emit()

    @Slot(Event)
    def _on_axis_changed(self, event):
        # slot for external events to update the current frame
        if event.axis == self.axis and hasattr(event, 'value'):
            self.current = event.value
