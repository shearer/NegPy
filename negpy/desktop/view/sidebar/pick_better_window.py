"""Separate, modeless window for the Pick Better mode.

Shows three equal frames in a row: the current committed edit in the centre, bracketed by
the two proposed candidates (the stronger on the left, the weaker on the right). Each frame is
captioned above: "A" over the stronger left, "Skip" over the centre and "B" over the weaker
right. The in-flight parameter name floats above the centre "Skip" caption and is always shown.
A click on a candidate commits it; a click on the centre current photo rejects both candidates
and advances to the next proposal; Undo re-shows the pair the last pick came from; OK ends the
mode and closes the window. A hover draws a red selection ring around the frame under the cursor.
A Difference slider widens or narrows the A/B spread live, and a Custom Routing button opens a
window that reorders which parameter each round proposes. The window floats over the editor panels
and each label letterboxes its frame, so a preview keeps its aspect ratio — it is never stretched
and never width-capped.
"""

from typing import Optional

from PyQt6.QtCore import Qt, QPoint, QRect, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from negpy.desktop.view.sidebar.roi_image import RoiImageLabel
from negpy.desktop.view.styles.templates import (
    caption_label,
    labeled_action,
    pin_dialog_default,
)
from negpy.desktop.view.widgets.dialog_geometry import remember_dialog_geometry
from negpy.desktop.view.widgets.floating_panel import float_over_app
from negpy.desktop.view.widgets.sliders import CompactSlider

#: The window sits one notch smaller than the main window: 0.8 of its size, floored so a
#: small main window does not shrink the three previews below a usable size.
_SIZE_FACTOR = 0.8
#: Floor for the 0.8x-main size: three full previews plus room for the name and the control row.
_MIN_W, _MIN_H = 1200, 760
#: Gap between a caption's bottom edge and its photo's top edge.
_CAPTION_GAP = 4
#: The move summary's clear delay and its margin from the panel's top edge.
_MOVE_CLEAR_MS = 3000
_MOVE_TOP_MARGIN = 12


class PickBetterWindow(QDialog):
    """Modeless window for Pick Better: the current edit centred, the two candidates either side."""

    picked = pyqtSignal(int)  # side: 0 = left (stronger), 1 = right (weaker)
    rejected = pyqtSignal()
    undoRequested = pyqtSignal()
    closed = pyqtSignal()
    diffChanged = pyqtSignal(float)  # the Difference slider's A/B spread multiplier
    customRoutingClicked = pyqtSignal()  # open the Custom Routing window

    def __init__(self, parent=None, *, repo=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pick Better")
        self.setModal(False)
        float_over_app(self)  # must precede the first show(): the panel flags hide a visible window
        self._last_main_geo: Optional[QRect] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Three equal frames in a row: the current committed edit in the centre, bracketed by the
        # two candidates (the stronger on the left, the weaker on the right).
        self._image_panel = QWidget(self)
        image_row = QHBoxLayout(self._image_panel)
        image_row.setSpacing(10)
        self.left_label = RoiImageLabel()
        self.current_label = RoiImageLabel()
        self.right_label = RoiImageLabel()
        for label in (self.left_label, self.current_label, self.right_label):
            label.roi_mode = False  # a tap emits `clicked`, not a calibration crosshair
            label.hover_ring = True  # a hover draws the red selection ring
        image_row.addWidget(self.left_label, 1)
        image_row.addWidget(self.current_label, 1)
        image_row.addWidget(self.right_label, 1)
        layout.addWidget(self._image_panel, 1)

        # The A/Skip/B captions are children of the panel, repositioned to each photo's top edge on
        # resize and on a new frame, so they hug the photo (a layout row would sit at the panel's top).
        self._cap_left = caption_label("A")
        self._cap_center = caption_label("Skip")
        self._cap_right = caption_label("B")
        for cap in (self._cap_left, self._cap_center, self._cap_right):
            cap.setParent(self._image_panel)
            cap.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # The in-flight parameter name floats above the centre "Skip" caption, always shown. It
        # passes mouse events through, so a click in its band still reaches the photo beneath.
        self._name_label = caption_label("")
        self._name_label.setParent(self._image_panel)
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        # The move summary floats at the panel's top: the "Name +delta" line shown for a few
        # seconds after a left or right pick. A single-shot timer clears it.
        self._move_label = caption_label("")
        self._move_label.setParent(self._image_panel)
        self._move_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._move_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._move_timer = QTimer(self)
        self._move_timer.setSingleShot(True)
        self._move_timer.timeout.connect(lambda: self._move_label.setText(""))

        # A control row under the photos: the Difference slider widens or narrows the A/B spread
        # live, and the Custom Routing button opens a window to reorder the round order.
        control_row = QHBoxLayout()
        self._diff_slider = CompactSlider("Difference", 0.25, 4.0, 1.0, step=0.05, unit="×")
        self._diff_slider.valueChanged.connect(self.diffChanged)
        self._custom_routing_btn = labeled_action(
            "fa5s.route", "Custom Routing", "Open the window that reorders which parameter each round proposes"
        )
        self._custom_routing_btn.clicked.connect(self.customRoutingClicked)
        control_row.addWidget(self._diff_slider, 1)
        control_row.addWidget(self._custom_routing_btn)
        layout.addLayout(control_row)

        # Bottom-right: Undo (re-show the previous pair) and OK (close = end the mode).
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.undo_btn = labeled_action("fa5s.undo", " Undo", "Revert the last pick and re-show that pair")
        self.ok_btn = labeled_action("fa5s.check", " OK", "Finish Pick Better and close the window", primary=True)
        button_row.addWidget(self.undo_btn)
        button_row.addWidget(self.ok_btn)
        layout.addLayout(button_row)

        self.left_label.clicked.connect(lambda _fx, _fy: self.picked.emit(0))
        self.right_label.clicked.connect(lambda _fx, _fy: self.picked.emit(1))
        self.current_label.clicked.connect(lambda _fx, _fy: self.rejected.emit())
        self.ok_btn.clicked.connect(lambda: self.close())
        self.undo_btn.clicked.connect(lambda: self.undoRequested.emit())

        # Pin OK as the dialog's one Enter target; Undo opts out.
        pin_dialog_default(self.ok_btn, self.undo_btn)

        remember_dialog_geometry(self, repo, "pick_better")
        self._schedule_reposition()

    def set_images(self, current: QPixmap, left: QPixmap, right: QPixmap) -> None:
        """Push the three rendered frames onto their labels (each letterboxes to fit)."""
        self.current_label.set_frame(current)
        self.left_label.set_frame(left)
        self.right_label.set_frame(right)
        self._schedule_reposition()

    def set_param_name(self, text: str) -> None:
        """The always-on in-flight parameter name, shown above the centre Skip caption."""
        self._name_label.setText(text)
        self._schedule_reposition()

    def show_move(self, text: str) -> None:
        """Show the "Name +delta" move summary for a few seconds, then clear it."""
        self._move_label.setText(text)
        self._reposition_move_label()
        self._move_timer.start(_MOVE_CLEAR_MS)

    def _reposition_move_label(self) -> None:
        """Centre the move summary at the panel's top; it hides itself when empty."""
        if not self._move_label.text():
            self._move_label.hide()
            return
        self._move_label.adjustSize()
        self._move_label.show()
        x = (self._image_panel.width() - self._move_label.width()) // 2
        self._move_label.move(max(0, x), _MOVE_TOP_MARGIN)
        self._move_label.raise_()

    def _reposition_captions(self) -> None:
        """Sit each caption just above its photo's top edge, tracking a resize or a new frame; the
        photo's top is a paint-time letterbox position, not a fixed layout slot."""
        for label, cap in (
            (self.left_label, self._cap_left),
            (self.current_label, self._cap_center),
            (self.right_label, self._cap_right),
        ):
            rect = label.frame_rect()
            if rect is None:
                cap.hide()
                continue
            cap.adjustSize()
            cap.show()
            origin = label.mapTo(self._image_panel, QPoint(0, 0))
            x = origin.x() + rect.x() + (rect.width() - cap.width()) // 2
            y = origin.y() + rect.y() - cap.height() - _CAPTION_GAP
            cap.move(x, y)
            # The parameter name floats above the centre "Skip" caption, centred on the same photo.
        if self._cap_center.isVisible():
            self._name_label.adjustSize()
            self._name_label.show()
            x = self._cap_center.x() + (self._cap_center.width() - self._name_label.width()) // 2
            y = self._cap_center.y() - self._name_label.height() - _CAPTION_GAP
            self._name_label.move(x, y)
            self._name_label.raise_()
        else:
            self._name_label.hide()
        # The move summary floats at the panel's top; re-centre it on every resize and new frame.
        self._reposition_move_label()

    def _schedule_reposition(self) -> None:
        # A label gets its final size only after the layout runs, which is after this event
        # returns; defer so frame_rect() reads settled geometry, not a stale (too-small) size.
        QTimer.singleShot(0, self._reposition_captions)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._schedule_reposition()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._schedule_reposition()

    def size_and_center_to(self, main: QWidget) -> None:
        """One notch smaller than, and centred on, the main window — 0.8 of its current size,
        floored so a small main window does not shrink the previews below a usable size. A
        re-centre runs only when the main window moved or resized, so a pick (which leaves the
        main window untouched) never resets a position the user has set."""
        geo = main.geometry()
        if geo == self._last_main_geo:
            return
        self._last_main_geo = geo
        w = max(int(geo.width() * _SIZE_FACTOR), _MIN_W)
        h = max(int(geo.height() * _SIZE_FACTOR), _MIN_H)
        self.resize(w, h)
        self.move(geo.x() + (geo.width() - w) // 2, geo.y() + (geo.height() - h) // 2)

    def done(self, result: int) -> None:
        # Esc reaches here without a closeEvent, so the session cleanup hangs off done().
        self.closed.emit()
        super().done(result)
