#!/usr/bin/env python3
import sys, os, sqlite3, time
from PySide6.QtCore import Qt, QUrl, QEvent, QTimer, QPoint
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QLabel, QStackedWidget, QSlider,
    QMessageBox
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

# ——— Constants ———
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, 'positions.db')
RESUME_STATUS   = QMediaPlayer.MediaStatus.BufferedMedia
END_OF_MEDIA    = QMediaPlayer.MediaStatus.EndOfMedia
WATCH_THRESH    = 45_000     # ms before end to mark watched
MIN_PROGRESS_S  = 15         # s watched to show “in-progress” dot
COUNT_MS        = 5_000      # ms before end to start countdown
COUNT_START     = 5          # seconds of countdown

# ——— DB helpers ———
def init_db():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''
      CREATE TABLE IF NOT EXISTS watch_positions(
        path TEXT PRIMARY KEY,
        last_pos REAL DEFAULT 0,
        watched INTEGER DEFAULT 0
      )''')
    c.execute("PRAGMA table_info(watch_positions)")
    if 'watched' not in [r[1] for r in c.fetchall()]:
        c.execute("ALTER TABLE watch_positions ADD COLUMN watched INTEGER DEFAULT 0")
    conn.commit(); conn.close()

def load_pos(path):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT last_pos,watched FROM watch_positions WHERE path=?", (path,))
    row = c.fetchone(); conn.close()
    return (row[0], bool(row[1])) if row else (0.0, False)

def save_pos(path, secs, wat=None):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    if wat is None:
        c.execute('''
          INSERT INTO watch_positions(path,last_pos)
          VALUES(?,?)
          ON CONFLICT(path) DO UPDATE SET last_pos=excluded.last_pos
        ''', (path, secs))
    else:
        c.execute('''
          INSERT INTO watch_positions(path,last_pos,watched)
          VALUES(?,?,?)
          ON CONFLICT(path) DO UPDATE SET last_pos=excluded.last_pos, watched=excluded.watched
        ''', (path, secs, int(wat)))
    conn.commit(); conn.close()

# ——— File scanning ———
def scan_shows():
    shows=[]; local=os.path.join(BASE_DIR,'MyShows')
    if os.path.isdir(local):
        for d in sorted(os.listdir(local)):
            p=os.path.join(local,d)
            if os.path.isdir(p): shows.append((d,p))
    for base in ('/media/deck','/run/media/deck'):
        if os.path.isdir(base):
            for u in os.listdir(base):
                p0=os.path.join(base,u,'MyShows')
                if os.path.isdir(p0):
                    for d in sorted(os.listdir(p0)):
                        p=os.path.join(p0,d)
                        if os.path.isdir(p): shows.append((d,p))
    return shows

def scan_eps(path):
    return [
      (os.path.splitext(f)[0], os.path.join(path,f))
      for f in sorted(os.listdir(path))
      if f.lower().endswith('.mp4')
    ]

# ——— Countdown toast ———
class CountdownToast(QWidget):
    def __init__(self, video):
        super().__init__(None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.lbl = QLabel(self)
        self.lbl.setStyleSheet("""
            background:rgba(0,0,0,200);
            color:white;
            font-size:18px;
            padding:8px;
            border-radius:5px;
        """)
        self.hide()
        self.video = video

    def show_count(self, sec):
        self.lbl.setText(f"Next episode in {sec}")
        self.lbl.adjustSize()
        bw, bh = self.lbl.width(), self.lbl.height()
        vw, vh = self.video.width(), self.video.height()
        tp = self.video.mapToGlobal(QPoint(0,0))
        x = tp.x() + vw - bw - 20
        y = tp.y() + vh - bh - 20
        self.setGeometry(x, y, bw, bh)
        self.show()

    def hide_toast(self):
        self.hide()

# ——— Clickable video ———
class ClickableVideo(QVideoWidget):
    def mouseDoubleClickEvent(self, ev):
        w = self.window()
        if hasattr(w,'toggle_fullscreen'): w.toggle_fullscreen()
        super().mouseDoubleClickEvent(ev)

# ——— Main streamer ———
class Streamer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mini Netflix"); self.resize(900,600)
        init_db()

        # playback + countdown state
        self.next_resume = 0.0
        self.duration    = 0
        self.cd_started  = False
        self.cd_sec      = COUNT_START

        self.cd_timer = QTimer(self)
        self.cd_timer.setInterval(1000)
        self.cd_timer.timeout.connect(self._tick)

        QApplication.instance().installEventFilter(self)
        self._dark_theme()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self._build_show_list()
        self._build_ep_list()
        self._build_player()

    def _dark_theme(self):
        QApplication.setStyle("Fusion"); p=QPalette()
        p.setColor(QPalette.Window, QColor(30,30,30))
        p.setColor(QPalette.WindowText, QColor(220,220,220))
        p.setColor(QPalette.Base, QColor(20,20,20))
        p.setColor(QPalette.AlternateBase, QColor(45,45,45))
        p.setColor(QPalette.Text, QColor(220,220,220))
        p.setColor(QPalette.Button, QColor(45,45,45))
        p.setColor(QPalette.ButtonText, QColor(220,220,220))
        p.setColor(QPalette.Highlight, QColor(100,100,180))
        p.setColor(QPalette.HighlightedText, QColor(220,220,220))
        QApplication.instance().setPalette(p)

    # — Show List View —
    def _build_show_list(self):
        container = QWidget()
        v = QVBoxLayout(container)
        h = QHBoxLayout()
        h.addWidget(QLabel("Select a Show"))
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_shows)
        h.addWidget(btn_refresh, alignment=Qt.AlignRight)
        v.addLayout(h)

        self.show_list = QListWidget()
        self.show_list.itemActivated.connect(self.on_show)
        v.addWidget(self.show_list)

        self.stack.addWidget(container)
        self.refresh_shows()

    def refresh_shows(self):
        self.show_list.clear()
        for name,_ in scan_shows():
            self.show_list.addItem(name)

    def on_show(self, item):
        self.current_show = next(p for n,p in scan_shows() if n==item.text())
        self.refresh_episodes()
        self.stack.setCurrentIndex(1)

    # — Episode List View —
    def _build_ep_list(self):
        container = QWidget()
        v = QVBoxLayout(container)

        h = QHBoxLayout()
        back = QPushButton("← Back to Shows")
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        h.addWidget(back)

        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_episodes)
        h.addWidget(btn_refresh, alignment=Qt.AlignRight)
        v.addLayout(h)

        self.ep_list = QListWidget()
        self.ep_list.itemActivated.connect(self.on_ep)
        v.addWidget(self.ep_list)

        self.stack.addWidget(container)

    def refresh_episodes(self):
        self.ep_list.clear()
        for title, path in scan_eps(self.current_show):
            lp, wat = load_pos(path)
            pre = "✔️ " if wat else ("🔸 " if lp >= MIN_PROGRESS_S else "")
            self.ep_list.addItem(pre + title)

    def on_ep(self, item):
        idx = self.ep_list.currentRow()
        title, path = scan_eps(self.current_show)[idx]
        lp, wat = load_pos(path)
        self.next_resume = 0.0 if wat else lp
        self.title_lbl.setText(title)
        self.start_play(path)

    # — Player View —
    def _build_player(self):
        container = QWidget()
        pv = QVBoxLayout(container)
        pv.setContentsMargins(0,0,0,0)

        # Controls bar
        self.controls = QWidget()
        cl = QVBoxLayout(self.controls)
        cl.setContentsMargins(5,5,5,5)

        tb = QHBoxLayout()
        exit_btn = QPushButton("Exit")
        exit_btn.clicked.connect(self.confirm_exit)
        tb.addWidget(exit_btn)
        tb.addStretch()
        self.title_lbl = QLabel("")
        tb.addWidget(self.title_lbl)
        tb.addStretch()
        cl.addLayout(tb)

        # Video + toast
        self.video = ClickableVideo(self)
        pv.addWidget(self.video, 1)
        self.toast = CountdownToast(self.video)

        # Time & slider
        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setAlignment(Qt.AlignCenter)
        cl.addWidget(self.time_lbl)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0,1000)
        self.slider.sliderMoved.connect(self.seek)
        cl.addWidget(self.slider)

        # Playback buttons
        row = QHBoxLayout()
        for txt, fn in (
            ("Prev Ep", self.prev_ep),
            ("<< 5s",   self.rewind),
            ("Pause",   self.play_pause),
            ("5s >>",   self.skip),
            ("Next Ep", self.next_ep),
            ("Fullscreen", self.toggle_fullscreen)
        ):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
            if fn == self.play_pause: self.play_btn = b
            if fn == self.toggle_fullscreen: self.fs_btn = b
        cl.addLayout(row)

        # Volume
        vr = QHBoxLayout()
        vr.addWidget(QLabel("Vol"))
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0,100)
        self.vol.setValue(100)
        self.vol.valueChanged.connect(lambda v: self.audio_out.setVolume(v/100))
        vr.addWidget(self.vol)
        cl.addLayout(vr)

        pv.addWidget(self.controls)
        self.stack.addWidget(container)

        # Media player setup
        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.audio_out.setVolume(1.0)
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(lambda d: setattr(self,'duration',d))

    def start_play(self, path):
        self.current_path = path
        self.stack.setCurrentIndex(2)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.play_btn.setText("Pause")
        self.cd_started = False
        self.toast.hide_toast()
        self.cd_timer.stop()

    def on_pos(self, pos):
        # hide toast if rewind above threshold
        if self.cd_started and self.duration and pos < self.duration - COUNT_MS:
            self.cd_started = False
            self.cd_timer.stop()
            self.toast.hide_toast()

        # start countdown
        if not self.cd_started and self.duration and pos >= self.duration - COUNT_MS:
            self.cd_started = True
            self.cd_sec = COUNT_START
            self.toast.show_count(self.cd_sec)
            self.cd_timer.start()

        # update slider/time
        if self.duration:
            frac = pos / self.duration
            self.slider.blockSignals(True)
            self.slider.setValue(int(frac*1000))
            self.slider.blockSignals(False)

        cur = time.strftime('%M:%S', time.gmtime(pos/1000))
        rem = time.strftime('%M:%S', time.gmtime((self.duration-pos)/1000))
        self.time_lbl.setText(f"{cur} / -{rem}")

        if pos >= self.duration - WATCH_THRESH:
            save_pos(self.current_path, pos/1000, wat=True)

    def _tick(self):
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        self.cd_sec -= 1
        if self.cd_sec > 0:
            self.toast.show_count(self.cd_sec)
        else:
            self.cd_timer.stop()

    def on_status(self, status):
        if status == RESUME_STATUS and self.next_resume:
            self.player.setPosition(int(self.next_resume*1000))
            self.next_resume = 0.0
        elif status == END_OF_MEDIA:
            self.toast.hide_toast()
            self.next_ep()

    def seek(self, v):
        if self.duration:
            self.player.setPosition(int((v/1000)*self.duration))

    def rewind(self):
        self.player.setPosition(max(0, self.player.position()-5000))
        if self.player.position() < self.duration - WATCH_THRESH:
            save_pos(self.current_path, self.player.position()/1000, wat=False)

    def skip(self):
        self.player.setPosition(min(self.duration, self.player.position()+5000))
        if self.player.position() < self.duration - WATCH_THRESH:
            save_pos(self.current_path, self.player.position()/1000, wat=False)

    def play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play_btn.setText("Play")
            self.cd_timer.stop()
        else:
            self.player.play()
            self.play_btn.setText("Pause")
            if self.cd_started:
                self.cd_timer.start()

    def prev_ep(self):
        eps = scan_eps(self.current_show)
        paths = [p for _,p in eps]
        i = paths.index(self.current_path)
        if i > 0:
            t,p = eps[i-1]; lp,wat = load_pos(p)
            self.next_resume = 0.0 if wat else lp
            self.title_lbl.setText(t)
            self.start_play(p)

    def next_ep(self):
        eps = scan_eps(self.current_show)
        paths = [p for _,p in eps]
        i = paths.index(self.current_path)
        if i < len(paths)-1:
            t,p = eps[i+1]; lp,wat = load_pos(p)
            self.next_resume = 0.0 if wat else lp
            self.title_lbl.setText(t)
            self.start_play(p)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal(); self.controls.show(); self.fs_btn.setText("Fullscreen")
        else:
            self.showFullScreen(); self.controls.hide(); self.fs_btn.setText("Exit FS")

    def confirm_exit(self):
        if self.isFullScreen():
            self.toggle_fullscreen()
            return
        self.player.pause()
        self.play_btn.setText("Play")
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Exit Show?")
        dlg.setText("Are you sure you want to exit this show?")
        dlg.setStandardButtons(QMessageBox.Yes|QMessageBox.No)
        dlg.setDefaultButton(QMessageBox.No)
        if dlg.exec() == QMessageBox.Yes:
            pos = self.player.position()
            save_pos(self.current_path, pos/1000, wat=(pos >= self.duration - WATCH_THRESH))
            self.player.stop()
            self.stack.setCurrentIndex(1)
        else:
            self.player.play()
            self.play_btn.setText("Pause")

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress:
            k, idx = ev.key(), self.stack.currentIndex()
            if idx == 2 and k in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self.play_pause(); return True
            if k == Qt.Key_Escape:
                if idx == 2:
                    self.confirm_exit(); return True
                if idx == 1:
                    self.stack.setCurrentIndex(0); return True
            if idx == 2 and k == Qt.Key_Left:
                self.rewind(); return True
            if idx == 2 and k == Qt.Key_Right:
                self.skip(); return True
        return super().eventFilter(obj, ev)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = Streamer()
    w.show()
    sys.exit(app.exec())
