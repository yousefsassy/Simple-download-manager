from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sdm.downloader import DownloadManager
from sdm.models import DownloadJob, DownloadStatus
from sdm.storage import Storage
from sdm.utils import format_bytes, format_duration


class DownloadBridge(QObject):
    job_changed = Signal(object)
    add_finished = Signal()
    add_failed = Signal(str)
    add_succeeded = Signal(str)


STATUS_COLORS = {
    DownloadStatus.QUEUED: QColor("#7dd3fc"),
    DownloadStatus.DOWNLOADING: QColor("#86efac"),
    DownloadStatus.PAUSED: QColor("#fde68a"),
    DownloadStatus.COMPLETED: QColor("#a7f3d0"),
    DownloadStatus.FAILED: QColor("#fca5a5"),
    DownloadStatus.CANCELED: QColor("#cbd5e1"),
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Simple Download Manager")
        self.manager = DownloadManager(Storage("sdm.sqlite3"), default_save_dir="downloads")
        self.bridge = DownloadBridge()
        self.save_dir = Path("downloads")
        self.selected_job_id: str | None = None

        self._build_ui()
        self.bridge.job_changed.connect(self._on_job_changed)
        self.bridge.add_finished.connect(lambda: self.url_input.setEnabled(True))
        self.bridge.add_failed.connect(self._show_add_error)
        self.bridge.add_succeeded.connect(self._on_add_succeeded)
        self.manager.add_callback(self.bridge.job_changed.emit)
        self._apply_style()
        self._refresh()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(700)

    def _build_ui(self) -> None:
        root = QWidget()
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(22, 24, 22, 24)
        side_layout.setSpacing(18)

        title = QLabel("SDM")
        title.setObjectName("brand")
        subtitle = QLabel("Segmented Download Manager")
        subtitle.setObjectName("subtitle")
        side_layout.addWidget(title)
        side_layout.addWidget(subtitle)
        side_layout.addSpacing(8)

        self.stats_total = QLabel("0 downloads")
        self.stats_active = QLabel("0 active")
        self.stats_done = QLabel("0 completed")
        for stat in (self.stats_total, self.stats_active, self.stats_done):
            stat.setObjectName("stat")
            side_layout.addWidget(stat)
        side_layout.addStretch(1)

        self.folder_label = QLabel(str(self.save_dir))
        self.folder_label.setObjectName("folder")
        self.folder_label.setWordWrap(True)
        folder_btn = QPushButton("Folder")
        folder_btn.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        folder_btn.clicked.connect(self._choose_folder)
        side_layout.addWidget(self.folder_label)
        side_layout.addWidget(folder_btn)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(26, 24, 26, 24)
        content_layout.setSpacing(18)

        header = QLabel("Downloads")
        header.setObjectName("pageTitle")

        input_panel = QFrame()
        input_panel.setObjectName("inputPanel")
        input_layout = QGridLayout(input_panel)
        input_layout.setContentsMargins(16, 14, 16, 14)
        input_layout.setHorizontalSpacing(10)
        input_layout.setVerticalSpacing(10)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/file.zip")
        self.url_input.returnPressed.connect(self._add_download)
        self.segment_spin = QSpinBox()
        self.segment_spin.setRange(1, 16)
        self.segment_spin.setValue(4)
        self.segment_spin.setPrefix("Segments ")
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 10)
        self.retry_spin.setValue(3)
        self.retry_spin.setPrefix("Retries ")
        self.active_jobs_spin = QSpinBox()
        self.active_jobs_spin.setRange(1, 8)
        self.active_jobs_spin.setValue(self.manager.max_active_downloads)
        self.active_jobs_spin.setPrefix("Active ")
        self.active_jobs_spin.valueChanged.connect(self.manager.set_max_active_downloads)
        add_btn = QPushButton("Add")
        add_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowDown))
        add_btn.clicked.connect(self._add_download)

        input_layout.addWidget(self.url_input, 0, 0)
        input_layout.addWidget(self.segment_spin, 0, 1)
        input_layout.addWidget(self.retry_spin, 0, 2)
        input_layout.addWidget(self.active_jobs_spin, 0, 3)
        input_layout.addWidget(add_btn, 0, 4)
        input_layout.setColumnStretch(0, 1)

        action_bar = QHBoxLayout()
        action_bar.setSpacing(8)
        self.start_btn = self._action_button("Start", QStyle.SP_MediaPlay, self._start_selected)
        self.pause_btn = self._action_button("Pause", QStyle.SP_MediaPause, self._pause_selected)
        self.resume_btn = self._action_button("Resume", QStyle.SP_MediaPlay, self._resume_selected)
        self.cancel_btn = self._action_button("Cancel", QStyle.SP_DialogCancelButton, self._cancel_selected)
        self.retry_btn = self._action_button("Retry", QStyle.SP_BrowserReload, self._retry_selected)
        for button in (
            self.start_btn,
            self.pause_btn,
            self.resume_btn,
            self.cancel_btn,
            self.retry_btn,
        ):
            action_bar.addWidget(button)
        action_bar.addStretch(1)

        self.tabs = QTabWidget()
        self.queue_table = self._make_table()
        self.history_table = self._make_table()
        self.tabs.addTab(self.queue_table, "Queue")
        self.tabs.addTab(self.history_table, "History")

        self.detail_label = QLabel("Select a download")
        self.detail_label.setObjectName("details")
        self.detail_label.setWordWrap(True)

        content_layout.addWidget(header)
        content_layout.addWidget(input_panel)
        content_layout.addLayout(action_bar)
        content_layout.addWidget(self.tabs, 1)
        content_layout.addWidget(self.detail_label)

        shell.addWidget(sidebar)
        shell.addWidget(content, 1)
        self.setCentralWidget(root)

    def _action_button(self, text: str, icon: QStyle.StandardPixmap, slot) -> QPushButton:
        button = QPushButton(text)
        button.setIcon(self.style().standardIcon(icon))
        button.clicked.connect(slot)
        return button

    def _make_table(self) -> QTableWidget:
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(["File", "Status", "Progress", "Size", "Speed", "ETA", "URL"])
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.itemSelectionChanged.connect(self._capture_selection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        return table

    def _add_download(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            return

        save_dir = self.save_dir
        segments = self.segment_spin.value()
        retries = self.retry_spin.value()
        self.url_input.setEnabled(False)

        def worker() -> None:
            try:
                job = self.manager.add_download(
                    url,
                    save_dir=save_dir,
                    segments=segments,
                    retries=retries,
                )
                self.manager.start(job.id)
                self.bridge.add_succeeded.emit(job.id)
            except Exception as exc:
                self.bridge.add_failed.emit(str(exc))
            finally:
                self.bridge.add_finished.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose download folder", str(self.save_dir))
        if folder:
            self.save_dir = Path(folder)
            self.folder_label.setText(str(self.save_dir))

    def _capture_selection(self) -> None:
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        selected = table.selectedItems()
        if selected:
            item = table.item(selected[0].row(), 0)
            self.selected_job_id = item.data(Qt.UserRole)
            self._update_details()

    def _selected_job(self) -> DownloadJob | None:
        self._sync_selection_from_visible_table()
        if not self.selected_job_id:
            return None
        return self.manager.get_job(self.selected_job_id)

    def _start_selected(self) -> None:
        job = self._selected_job_or_active()
        if job:
            self.manager.start(job.id)

    def _pause_selected(self) -> None:
        job = self._selected_job_or_active()
        if job:
            self.manager.pause(job.id)

    def _resume_selected(self) -> None:
        job = self._selected_job_or_active()
        if job:
            self.manager.resume(job.id)

    def _cancel_selected(self) -> None:
        job = self._selected_job_or_active()
        if job:
            self.manager.cancel(job.id)

    def _retry_selected(self) -> None:
        job = self._selected_job_or_active()
        if job:
            self.manager.retry(job.id)

    def _on_job_changed(self, _job: object) -> None:
        self._refresh()

    def _on_add_succeeded(self, job_id: str) -> None:
        self.selected_job_id = job_id
        self.url_input.clear()
        self.tabs.setCurrentWidget(self.queue_table)
        self._refresh()

    def _show_add_error(self, message: str) -> None:
        QMessageBox.warning(self, "Download error", message)

    def _sync_selection_from_visible_table(self) -> None:
        table = self.tabs.currentWidget()
        if not isinstance(table, QTableWidget):
            return
        selected_rows = table.selectionModel().selectedRows()
        if not selected_rows:
            return
        item = table.item(selected_rows[0].row(), 0)
        if item:
            self.selected_job_id = item.data(Qt.UserRole)

    def _selected_job_or_active(self) -> DownloadJob | None:
        self._sync_selection_from_visible_table()
        if self.selected_job_id:
            job = self.manager.get_job(self.selected_job_id)
            if job:
                return job

        for job in self.manager.list_jobs():
            if job.status == DownloadStatus.DOWNLOADING:
                self.selected_job_id = job.id
                return job
        return None

    def _refresh(self) -> None:
        jobs = self.manager.list_jobs()
        queue = [
            job
            for job in jobs
            if job.status
            not in {DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELED}
        ]
        history = [
            job
            for job in jobs
            if job.status in {DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELED}
        ]
        self._fill_table(self.queue_table, queue)
        self._fill_table(self.history_table, history)
        active = sum(1 for job in jobs if job.status == DownloadStatus.DOWNLOADING)
        done = sum(1 for job in jobs if job.status == DownloadStatus.COMPLETED)
        self.stats_total.setText(f"{len(jobs)} downloads")
        self.stats_active.setText(f"{active} active")
        self.stats_done.setText(f"{done} completed")
        self._update_details()

    def _fill_table(self, table: QTableWidget, jobs: list[DownloadJob]) -> None:
        table.blockSignals(True)
        table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            file_item = QTableWidgetItem(job.filename)
            file_item.setData(Qt.UserRole, job.id)
            table.setItem(row, 0, file_item)

            status_item = QTableWidgetItem(job.status.value.title())
            status_item.setForeground(QColor("#0f172a"))
            status_item.setBackground(STATUS_COLORS.get(job.status, QColor("#e2e8f0")))
            status_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 1, status_item)

            progress = QProgressBar()
            progress.setRange(0, 1000)
            progress.setValue(int(job.progress * 10))
            progress.setFormat(f"{job.progress:.1f}%")
            table.setCellWidget(row, 2, progress)

            table.setItem(row, 3, QTableWidgetItem(format_bytes(job.total_size)))
            table.setItem(row, 4, QTableWidgetItem(f"{format_bytes(job.speed_bps)}/s"))
            table.setItem(row, 5, QTableWidgetItem(format_duration(job.eta_seconds)))
            url_item = QTableWidgetItem(job.url)
            url_item.setToolTip(job.url)
            table.setItem(row, 6, url_item)

            if job.id == self.selected_job_id:
                table.selectRow(row)
        if jobs and not self.selected_job_id:
            self.selected_job_id = jobs[0].id
            table.selectRow(0)
        table.blockSignals(False)

    def _update_details(self) -> None:
        job = self._selected_job()
        if not job:
            self.detail_label.setText("Select a download")
            return
        error = f" | {job.error}" if job.error else ""
        self.detail_label.setText(
            f"{job.filename} | {job.status.value.title()} | "
            f"{format_bytes(job.bytes_downloaded)} of {format_bytes(job.total_size)} | "
            f"{job.segment_count} segment(s) | {job.final_path}{error}"
        )

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0b1120;
                color: #e5edf7;
                font-family: "Inter", "SF Pro Display", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            #sidebar {
                background: #111827;
                border-right: 1px solid #243244;
            }
            #brand {
                color: #f8fafc;
                font-size: 34px;
                font-weight: 800;
                letter-spacing: 0;
            }
            #subtitle, #folder {
                color: #94a3b8;
            }
            #stat {
                background: #182235;
                border: 1px solid #2c3b50;
                border-radius: 8px;
                padding: 10px;
                color: #dbeafe;
                font-weight: 600;
            }
            #pageTitle {
                font-size: 26px;
                font-weight: 800;
                color: #f8fafc;
            }
            #inputPanel {
                background: #111827;
                border: 1px solid #26364d;
                border-radius: 8px;
            }
            QLineEdit, QSpinBox {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 7px;
                padding: 9px 10px;
                color: #f8fafc;
                min-height: 22px;
            }
            QPushButton {
                background: #2563eb;
                border: 1px solid #3b82f6;
                border-radius: 7px;
                color: white;
                font-weight: 700;
                padding: 9px 14px;
                min-height: 22px;
            }
            QPushButton:hover {
                background: #1d4ed8;
            }
            QPushButton:pressed {
                background: #1e40af;
            }
            QTabWidget::pane {
                border: 1px solid #26364d;
                border-radius: 8px;
                background: #0f172a;
            }
            QTabBar::tab {
                background: #111827;
                border: 1px solid #26364d;
                padding: 9px 18px;
                color: #cbd5e1;
            }
            QTabBar::tab:selected {
                background: #1d4ed8;
                color: white;
            }
            QTableWidget {
                background: #0f172a;
                alternate-background-color: #131c2e;
                border: none;
                gridline-color: #1f2a3d;
                selection-background-color: #1e3a8a;
                selection-color: #f8fafc;
            }
            QHeaderView::section {
                background: #172033;
                color: #cbd5e1;
                border: none;
                border-bottom: 1px solid #26364d;
                padding: 8px;
                font-weight: 800;
            }
            QProgressBar {
                background: #1f2937;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #e5edf7;
                text-align: center;
                height: 18px;
            }
            QProgressBar::chunk {
                background: #22c55e;
                border-radius: 5px;
            }
            #details {
                color: #cbd5e1;
                background: #111827;
                border: 1px solid #26364d;
                border-radius: 8px;
                padding: 10px 12px;
            }
            """
        )
