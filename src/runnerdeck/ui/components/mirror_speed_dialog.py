"""pip mirror speed-test results (settings page): fastest first, offers the fastest one."""

from __future__ import annotations

from collections.abc import Sequence

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QGridLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, MessageBoxBase, StrongBodyLabel, SubtitleLabel

from runnerdeck.core import pip_mirror
from runnerdeck.core.models import MirrorSpeed

ERROR_COLOR = ("#c42b1c", "#ff99a4")  # light, dark


def format_speed(result: MirrorSpeed) -> str:
    speed = result.mb_per_s
    if speed is None:
        return f"失败：{result.error or '未知错误'}"
    return f"{speed:.2f} MB/s" if speed < 1 else f"{speed:.1f} MB/s"


def probe_note(proxy: str | None) -> str:
    route = f"经代理 {proxy}" if proxy else "未设置代理（系统代理或直连）"
    limit = pip_mirror.PROBE_MAX_BYTES // (1024 * 1024)
    return (
        f"每个源下载同一个文件，最多 {limit} MB 或 {pip_mirror.PROBE_MAX_SECONDS:g} 秒；"
        f"{route}，和 runner 里 pip 走的路线相同"
    )


class MirrorSpeedDialog(MessageBoxBase):
    """`best_url`: the fastest mirror when it is not the current one (the 使用 button), else
    None (only 关闭 is shown)."""

    def __init__(
        self,
        results: Sequence[MirrorSpeed],
        current: str | None,
        proxy: str | None,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        ranked = pip_mirror.ranked(results)
        current_value = pip_mirror.option_value(current)

        self.titleLabel = SubtitleLabel("pip 镜像源测速", self)
        self.noteLabel = CaptionLabel(probe_note(proxy), self)
        self.noteLabel.setWordWrap(True)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.noteLabel)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)
        self.rows: list[tuple[str, str]] = []  # (name, speed) as shown, for tests
        for row, result in enumerate(ranked):
            name = result.label
            if current_value is not None and result.index_url == current_value:
                name += "（当前）"
            name_label = StrongBodyLabel(name, self) if row == 0 else BodyLabel(name, self)
            speed_label = BodyLabel(format_speed(result), self)
            speed_label.setWordWrap(True)
            if result.mb_per_s is None:
                speed_label.setTextColor(*ERROR_COLOR)
            grid.addWidget(name_label, row, 0, Qt.AlignLeft | Qt.AlignTop)
            grid.addWidget(speed_label, row, 1, Qt.AlignLeft | Qt.AlignTop)
            self.rows.append((name, speed_label.text()))
        grid.setColumnStretch(1, 1)
        self.viewLayout.addLayout(grid)
        self.widget.setMinimumWidth(460)

        best = ranked[0] if ranked and ranked[0].mb_per_s is not None else None
        self.best_url = (
            best.index_url if best is not None and best.index_url != current_value else None
        )
        if best is not None and self.best_url is not None:
            self.yesButton.setText(f"使用{best.label}")
        else:
            self.yesButton.hide()
        self.cancelButton.setText("关闭")
