#!/usr/bin/env python3
import sys, os, sqlite3, time
from PySide6.QtCore import Qt, QUrl, QEvent, QTimer        # ← added QTimer here
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QListWidget, QLabel, QStackedWidget, QSlider,
    QMessageBox
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(BASE_DIR, 'positions.db')
RESUME_STATUS     = QMediaPlayer.MediaStatus.BufferedMedia
END_OF_MEDIA      = QMediaPlayer.MediaStatus.EndOfMedia
WATCHED_THRESH_MS = 45_000    # 45 seconds to mark watched
MIN_PROGRESS_S    = 15        # 15 seconds for showing in-progress

def init_db():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''
      CREATE TABLE IF NOT EXISTS watch_positions (
        path     TEXT PRIMARY KEY,
        last_pos REAL DEFAULT 0,
        watched  INTEGER DEFAULT 0
      )
    ''')
    c.execute("PRAGMA table_info(watch_positions)")
    cols = [r[1] for r in c.fetchall()]
    if 'watched' not in cols:
        c.execute("ALTER TABLE watch_positions ADD COLUMN watched INTEGER DEFAULT 0")
    conn.commit(); conn.close()

def load_pos(path):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT last_pos, watched FROM watch_positions WHERE path=?", (path,))
    row = c.fetchone(); conn.close()
    return (row[0], bool(row[1])) if row else (0.0, False)

def save_pos(path, secs, watched_flag=None):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    if watched_flag is None:
        c.execute('''
          INSERT INTO watch_positions(path,last_pos)
          VALUES(?,?)
          ON CONFLICT(path) DO UPDATE SET last_pos=excluded.last_pos
        ''', (path, secs))
    else:
        c.execute('''
          INSERT INTO watch_positions(path,last_pos,watched)
          VALUES(?,?,?)
          ON CONFLICT(path) DO UPDATE SET
            last_pos=excluded.last_pos,
            watched=excluded.watched
        ''', (path, secs, int(watched_flag)))
    conn.commit(); conn.close()

def scan_shows():
    shows=[]
    local=os.path.join(BASE_DIR,'MyShows')
    if os.path.isdir(local):
        for d in sorted(os.listdir(local)):
            p=os.path.join(local,d)
            if os.path.isdir(p): shows.append((d,p))
    for base in ('/media/deck','/run/media/deck'):
        if os.path.isdir(base):
            for sub in os.listdir(base):
                p0=os.path.join(base,sub,'MyShows')
                if os.path.isdir(p0):
                    for d in sorted(os.listdir(p0)):
                        p=os.path.join(p0,d)
                        if os.path.isdir(p): shows.append((d,p))
    return shows

def scan_eps(show_path):
    return [
        (os.path.splitext(f)[0], os.path.join(show_path,f))
        for f in sorted(os.listdir(show_path))
        if f.lower().endswith('.mp4')
    ]

class ClickableVideo(QVideoWidget):
    def mouseDoubleClickEvent(self, ev):
        win=self.window()
        if hasattr(win,'toggle_fullscreen'): win.toggle_fullscreen()
        super().mouseDoubleClickEvent(ev)

class Streamer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mini Netflix"); self.resize(900,600)
        init_db()
        self.is_fullscreen=False
        self.next_resume=0.0
        self.duration=0
        self.confirming=False

        app=QApplication.instance()
        app.installEventFilter(self)

        # Fusion + dark palette
        QApplication.setStyle("Fusion")
        p=QPalette()
        p.setColor(QPalette.Window,QColor(30,30,30))
        p.setColor(QPalette.WindowText,QColor(220,220,220))
        p.setColor(QPalette.Base,QColor(20,20,20))
        p.setColor(QPalette.AlternateBase,QColor(45,45,45))
        p.setColor(QPalette.ToolTipBase,QColor(220,220,220))
        p.setColor(QPalette.ToolTipText,QColor(20,20,20))
        p.setColor(QPalette.Text,QColor(220,220,220))
        p.setColor(QPalette.Button,QColor(45,45,45))
        p.setColor(QPalette.ButtonText,QColor(220,220,220))
        p.setColor(QPalette.Highlight,QColor(100,100,180))
        p.setColor(QPalette.HighlightedText,QColor(220,220,220))
        app.setPalette(p)

        # Views stack
        self.stack=QStackedWidget(); self.setCentralWidget(self.stack)

        # Show list
        self.show_list=QListWidget()
        for name,_ in scan_shows(): self.show_list.addItem(name)
        self.show_list.itemActivated.connect(self.on_show)
        w1=QWidget(); v1=QVBoxLayout(w1)
        v1.addWidget(QLabel("Select a Show")); v1.addWidget(self.show_list)
        self.stack.addWidget(w1)

        # Episode list
        self.ep_list=QListWidget()
        self.ep_list.itemActivated.connect(self.on_ep)
        w2=QWidget(); v2=QVBoxLayout(w2)
        b2=QPushButton("← Back to Shows"); b2.clicked.connect(lambda:self.stack.setCurrentIndex(0))
        v2.addWidget(b2); v2.addWidget(QLabel("Select an Episode")); v2.addWidget(self.ep_list)
        self.stack.addWidget(w2)

        # Player view
        player=QWidget(); pv=QVBoxLayout(player); pv.setContentsMargins(0,0,0,0)
        self.controls=QWidget(); cl=QVBoxLayout(self.controls); cl.setContentsMargins(5,5,5,5)
        # Top bar: Exit + Episode title
        tb=QHBoxLayout()
        self.btn_exit=QPushButton("Exit"); self.btn_exit.setFixedSize(70,28)
        self.btn_exit.clicked.connect(self.confirm_exit)
        tb.addWidget(self.btn_exit,alignment=Qt.AlignLeft)
        tb.addStretch()
        self.lbl_ep_title=QLabel("")  # Episode title
        self.lbl_ep_title.setStyleSheet("font-weight:bold; font-size:16px;")
        tb.addWidget(self.lbl_ep_title, alignment=Qt.AlignCenter)
        tb.addStretch()
        cl.addLayout(tb)

        # Video widget
        self.video=ClickableVideo(self); pv.addWidget(self.video,stretch=1)

        # Time label
        self.time_label=QLabel("00:00 / 00:00")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setStyleSheet("font-size:14px;")
        cl.addWidget(self.time_label)

        # Seek slider
        self.slider=QSlider(Qt.Horizontal); self.slider.setRange(0,1000)
        self.slider.sliderMoved.connect(self.seek); cl.addWidget(self.slider)

        # Controls row
        ctrl=QHBoxLayout()
        self.btn_prev=QPushButton("Prev Ep"); self.btn_prev.clicked.connect(self.prev_episode); ctrl.addWidget(self.btn_prev)
        self.btn_rew=QPushButton("<< 5s"); self.btn_rew.clicked.connect(self.rewind); ctrl.addWidget(self.btn_rew)
        self.btn_play=QPushButton("Pause"); self.btn_play.clicked.connect(self.play_pause); ctrl.addWidget(self.btn_play)
        self.btn_skip=QPushButton("5s >>"); self.btn_skip.clicked.connect(self.skip); ctrl.addWidget(self.btn_skip)
        self.btn_next=QPushButton("Next Ep"); self.btn_next.clicked.connect(self.next_episode); ctrl.addWidget(self.btn_next)
        self.btn_full=QPushButton("Fullscreen"); self.btn_full.clicked.connect(self.toggle_fullscreen); ctrl.addWidget(self.btn_full)
        cl.addLayout(ctrl)

        # Volume
        vol=QHBoxLayout(); vol.addWidget(QLabel("Vol"))
        self.vol_slider=QSlider(Qt.Horizontal); self.vol_slider.setRange(0,100); self.vol_slider.setValue(100)
        self.vol_slider.valueChanged.connect(self.set_volume); vol.addWidget(self.vol_slider); cl.addLayout(vol)

        pv.addWidget(self.controls); self.stack.addWidget(player)

        # Media player & audio
        self.player=QMediaPlayer(); self.audio_output=QAudioOutput(); self.audio_output.setVolume(1.0)
        self.player.setAudioOutput(self.audio_output); self.player.setVideoOutput(self.video)
        self.player.mediaStatusChanged.connect(self.on_status)
        self.player.positionChanged.connect(self.on_pos)
        self.player.durationChanged.connect(self.on_dur)

    def eventFilter(self,obj,ev):
        if ev.type()==QEvent.KeyPress and not self.confirming:
            k,idx=ev.key(),self.stack.currentIndex()
            if k in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space) and idx==2:
                self.play_pause(); return True
            if k==Qt.Key_Escape:
                if idx==2: self.confirm_exit()
                elif idx==1: self.stack.setCurrentIndex(0)
                return True
            if k==Qt.Key_Left and idx==2:
                self.rewind(); return True
            if k==Qt.Key_Right and idx==2:
                self.skip(); return True
        return super().eventFilter(obj,ev)

    def on_show(self,item):
        self.current_show=next(p for n,p in scan_shows() if n==item.text())
        self.ep_list.clear()
        for title,path in scan_eps(self.current_show):
            lp,wat=load_pos(path)
            prefix="✔️ " if wat else ("🔸 " if lp>=MIN_PROGRESS_S else "")
            self.ep_list.addItem(prefix+title)
        self.stack.setCurrentIndex(1)

    def on_ep(self,item):
        idx=self.ep_list.currentRow(); title,path=scan_eps(self.current_show)[idx]
        lp,wat=load_pos(path)
        self.next_resume=0.0 if wat else lp
        self.lbl_ep_title.setText(title)           # update title
        self.start_play(path)

    def start_play(self,path):
        self.current_path=path
        self.stack.setCurrentIndex(2)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play(); self.btn_play.setText("Pause")

    def on_status(self,st):
        if st==RESUME_STATUS and self.next_resume:
            self.player.setPosition(int(self.next_resume*1000)); self.next_resume=0.0
        elif st==END_OF_MEDIA:
            QTimer.singleShot(5000, self.next_episode)  # now works!

    def on_pos(self,pos):
        if self.duration:
            frac=pos/self.duration
            self.slider.blockSignals(True); self.slider.setValue(int(frac*1000))
            self.slider.blockSignals(False)
        cur=time.strftime('%M:%S',time.gmtime(pos/1000))
        rem=time.strftime('%M:%S',time.gmtime((self.duration-pos)/1000))
        self.time_label.setText(f"{cur} / -{rem}")
        if pos>=self.duration-WATCHED_THRESH_MS:
            save_pos(self.current_path,pos/1000,watched_flag=True)

    def on_dur(self,dur): self.duration=dur

    def seek(self,val):
        if self.duration: self.player.setPosition(int((val/1000)*self.duration))

    def rewind(self):
        self.player.setPosition(max(0,self.player.position()-5000))
        if self.player.position()<self.duration-WATCHED_THRESH_MS:
            save_pos(self.current_path,self.player.position()/1000,watched_flag=False)

    def skip(self):
        self.player.setPosition(min(self.duration,self.player.position()+5000))
        if self.player.position()<self.duration-WATCHED_THRESH_MS:
            save_pos(self.current_path,self.player.position()/1000,watched_flag=False)

    def play_pause(self):
        if self.player.playbackState()==QMediaPlayer.PlayingState:
            self.player.pause(); self.btn_play.setText("Play")
        else:
            self.player.play(); self.btn_play.setText("Pause")

    def set_volume(self,val):
        self.audio_output.setVolume(val/100)

    def prev_episode(self):
        eps=scan_eps(self.current_show); paths=[p for _,p in eps]; idx=paths.index(self.current_path)
        if idx>0:
            title, path= eps[idx-1]
            lp,wat=load_pos(path)
            self.next_resume=0.0 if wat else lp
            self.lbl_ep_title.setText(title)
            self.start_play(path)

    def next_episode(self):
        eps=scan_eps(self.current_show); paths=[p for _,p in eps]; idx=paths.index(self.current_path)
        if idx<len(paths)-1:
            title, path= eps[idx+1]
            lp,wat=load_pos(path)
            self.next_resume=0.0 if wat else lp
            self.lbl_ep_title.setText(title)
            self.start_play(path)

    def toggle_fullscreen(self):
        if not self.is_fullscreen:
            self.controls.hide(); self.showFullScreen(); self.btn_full.setText("Exit FS")
        else:
            self.showNormal(); self.controls.show(); self.btn_full.setText("Fullscreen")
        self.is_fullscreen=not self.is_fullscreen

    def confirm_exit(self):
        if self.is_fullscreen:
            self.toggle_fullscreen(); return
        self.confirming=True; self.player.pause(); self.btn_play.setText("Play")
        dlg=QMessageBox(self); dlg.setWindowTitle("Exit Show?"); dlg.setText("Are you sure you want to exit this show?")
        dlg.setStandardButtons(QMessageBox.Yes|QMessageBox.No); dlg.setDefaultButton(QMessageBox.No)
        ans=dlg.exec(); self.confirming=False
        if ans==QMessageBox.Yes: self.on_exit()
        else: self.player.play(); self.btn_play.setText("Pause")

    def on_exit(self):
        pos=self.player.position()
        if pos<self.duration-WATCHED_THRESH_MS:
            save_pos(self.current_path,pos/1000,watched_flag=False)
        else:
            save_pos(self.current_path,pos/1000)
        self.player.stop()
        if self.is_fullscreen: self.toggle_fullscreen()
        self.stack.setCurrentIndex(1)

if __name__=='__main__':
    app=QApplication(sys.argv)
    w=Streamer(); w.show()
    sys.exit(app.exec())
