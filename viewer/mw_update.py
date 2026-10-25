"""更新の確認と匿名レポート（MainWindow から切り出した Mixin）。

自動更新は凍結ビルドでのみ動く。匿名レポートは送信先 URL が設定されて
いて、かつ本人が同意したときだけ送る（入力値やファイル名は送らない）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QProgressDialog,
)

from . import settings_dialog as prefs
from .version import APP_VERSION, TELEMETRY_URL


class _Cancelled(Exception):
    """待ち画面でキャンセルされた。"""


class UpdateMixin:
    """更新の確認と、未対応フォームの匿名レポート。MainWindow に混ぜて使う。"""

    # --- 未対応フォーム機能の検知・報告 --------------------------------
    def _check_unsupported_govform(self, doc) -> None:
        """開いたフォームに未対応の入力補助があれば、そっと知らせる。

        官公庁フォームは共有 JS ライブラリで作られており、稀にこのアプリが
        まだ対応していない補助関数を使う。その場合は「選べない/丸がつかない」
        欄が出るため、ユーザーが気づけるよう通知し、報告用に書き出せるように
        アクションを有効化する。
        """
        self._unsupported_helpers = []
        try:
            names = doc.govform_unsupported_helpers()
        except Exception:  # noqa: BLE001
            names = []
        self._unsupported_helpers = names
        self.act_report_govform.setEnabled(bool(names))
        if names:
            self.statusBar().showMessage(
                f"このフォームには未対応の入力補助が {len(names)} 件あります。"
                "［リボン「表示」→ ヘルプ →「フォーム報告」］で書き出せます。", 8000)
            self._maybe_send_govform_telemetry(doc)

    def _telemetry_url(self) -> str:
        """テレメトリ送信先（version.py の定数を設定で上書き可）。空=送信しない。"""
        from .version import TELEMETRY_URL
        return self.settings.value("telemetry_url", "", str) or TELEMETRY_URL

    def _maybe_send_govform_telemetry(self, doc) -> None:
        """同意済みなら、未対応フォームの匿名レポートを自動送信する。

        送信先URL未設定なら何もしない（同意も尋ねない）。未決定なら初回だけ
        同意を尋ね、既定は「送らない」。送るのは関数名＋フォーム定義＋版＋指紋
        だけで、利用者の入力値・氏名・ファイル名は含めない。
        """
        from . import telemetry
        url = self._telemetry_url()
        if not url:
            return  # 配布者が受け口を設定していない → 送信機能そのものが無効
        state = telemetry.consent_state(self.settings)
        if state == "":  # 未決定 → 初回だけ同意を尋ねる
            state = "yes" if self._ask_telemetry_consent() else "no"
        if state != "yes":
            return
        try:
            payload = doc.govform_telemetry_payload(app_version=self._app_version())
        except Exception:  # noqa: BLE001
            payload = None
        if not payload:
            return
        fp = payload.get("form_fingerprint", "")
        if fp and telemetry.already_sent(self.settings, fp):
            return  # 同じフォームは一度だけ
        telemetry.send_async(url, payload)
        if fp:
            telemetry.mark_sent(self.settings, fp)
        self.statusBar().showMessage("未対応フォームの匿名レポートを送信しました", 4000)

    def _ask_telemetry_consent(self) -> bool:
        """初回の同意確認。既定（Escや×）は「送らない」。"""
        from . import telemetry
        box = QMessageBox(self)
        box.setWindowTitle("匿名レポートの送信")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "未対応の入力補助を含むフォームを開きました。\n"
            "改善のため、匿名レポートを開発元へ送信してもよろしいですか？")
        box.setInformativeText(
            "送信する内容: 未対応の関数名 / フォーム側の定義 / アプリ・ライブラリ版 /\n"
            "　フォームの指紋（同一判定用）\n"
            "送信しない内容: あなたが入力した値・氏名・住所等の個人情報・ファイル名\n\n"
            "この選択は［設定（Ctrl+,）→ 更新 →「未対応フォームの匿名レポートを送る」］でいつでも変更できます。")
        yes = box.addButton("送信する", QMessageBox.ButtonRole.YesRole)
        no = box.addButton("送信しない", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no)
        box.exec()
        agreed = box.clickedButton() is yes
        telemetry.set_consent(self.settings, agreed)
        self.act_telemetry_toggle.blockSignals(True)
        self.act_telemetry_toggle.setChecked(agreed)
        self.act_telemetry_toggle.blockSignals(False)
        return agreed

    def _app_version(self) -> str:
        from .version import APP_VERSION
        return APP_VERSION

    def _toggle_telemetry(self, on: bool) -> None:
        from . import telemetry
        telemetry.set_consent(self.settings, on)
        self.statusBar().showMessage(
            "匿名レポートの送信を{}にしました".format("オン" if on else "オフ"), 4000)

    def report_govform_unsupported(self) -> None:
        """未対応ヘルパーの診断テキストを保存する（報告用）。

        ネットワークへは送らない。書き出したファイルをユーザーが開発元へ
        送ることで対応追加できる、という導線。
        """
        if not self.doc.is_open:
            return
        names = getattr(self, "_unsupported_helpers", []) or []
        if not names:
            QMessageBox.information(
                self, "未対応フォーム機能の報告",
                "このフォームに未対応の入力補助は見つかりませんでした。")
            return
        base = "TriVReader_未対応フォーム機能.txt"
        if self.doc.path:
            base = os.path.splitext(os.path.basename(self.doc.path))[0] + "_未対応機能.txt"
        out, _ = QFileDialog.getSaveFileName(
            self, "報告用に保存", os.path.join(self._last_dir(), base),
            "テキスト (*.txt)")
        if not out:
            return
        try:
            from .version import APP_VERSION
            written = self.doc.write_govform_diagnostic(out, app_version=APP_VERSION)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "報告用に保存", f"書き出しに失敗しました:\n{exc}")
            return
        QMessageBox.information(
            self, "報告用に保存",
            f"未対応の入力補助 {len(written)} 件を書き出しました:\n{out}\n\n"
            "このファイルを開発元へお送りいただくと、対応追加を検討できます。\n"
            "（フォーム側の定義のみを含み、入力した値は含まれません）")

    def check_update_on_startup(self) -> None:
        """設定が有効なら、起動直後に静かに更新を確認する。"""
        if prefs.value(self.settings, "startup/check_update"):
            QTimer.singleShot(3000, self._silent_update_check)

    def _silent_update_check(self) -> None:
        """更新があるときだけステータスバーで知らせる（無ければ黙る）。

        通信でUIを止めないようワーカースレッドで確認し、結果だけ後から拾う。
        """
        import threading

        from . import updater

        if not updater.is_frozen():
            return
        url = updater.manifest_url(self.settings)
        if not url:
            return

        def _worker() -> None:
            try:
                self._pending_update = updater.check(url, timeout=8)
            except Exception:  # noqa: BLE001
                self._pending_update = None  # ネットワーク不通で起動を邪魔しない

        self._pending_update = None
        threading.Thread(target=_worker, daemon=True).start()
        QTimer.singleShot(9000, self._notify_pending_update)

    def _notify_pending_update(self) -> None:
        info = getattr(self, "_pending_update", None)
        if info:
            self.statusBar().showMessage(
                f"新しいバージョン {info.get('version', '')} があります"
                "（リボン「表示」→ ヘルプ →「更新を確認」から更新できます）", 15000)

    # --- 更新 / バージョン情報 ----------------------------------------
    def show_about(self) -> None:
        from . import updater
        from .version import APP_VERSION

        QMessageBox.information(
            self, "バージョン情報",
            f"TriV-Reader\nバージョン {APP_VERSION}\n"
            f"{'（凍結ビルド）' if updater.is_frozen() else '（ソース実行）'}",
        )

    def set_update_url(self) -> None:
        from . import updater

        cur = updater.manifest_url(self.settings)
        url, ok = QInputDialog.getText(
            self, "更新元URLを設定",
            "update.json の公開URL（GitHub Releases 等）:", text=cur
        )
        if ok:
            self.settings.setValue("update_url", url.strip())
            self.statusBar().showMessage("更新元URLを保存しました", 3000)

    def _pump(self, text: str | None, fn, dialog: QProgressDialog | None = None):
        """fn() を別スレッドで走らせ、終わるまで画面を動かし続けて結果を返す。

        遅い処理（通信・展開）を画面のスレッドで直接走らせると「応答なし」になる。
        text があれば、キャンセルできる待ち画面を出す（閉じられたら _Cancelled）。
        fn が投げた例外はそのまま投げ直す。
        """
        import threading
        import time

        box: dict = {}

        def work() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001  画面側で投げ直す
                box["error"] = exc

        own = dialog is None
        if own:
            dialog = QProgressDialog(text, "キャンセル", 0, 0, self)
            dialog.setWindowTitle("更新")
            dialog.setMinimumDuration(0)
            dialog.show()
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            QApplication.processEvents()
            if own and dialog.wasCanceled():
                dialog.close()
                raise _Cancelled()
            time.sleep(0.02)
        if own:
            dialog.close()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def check_for_update(self) -> None:
        from . import updater

        if not updater.is_frozen():
            QMessageBox.information(
                self, "更新の確認",
                "ソース実行では自動更新は不要です。\n"
                "依存の更新は update.ps1（pip 更新＋再ビルド）をご利用ください。",
            )
            return
        url = updater.manifest_url(self.settings)
        if not url:
            QMessageBox.information(
                self, "更新元が未設定",
                "更新元URLが設定されていません。\n"
                "［リボン「表示」→ ヘルプ →「更新元URL」］で update.json のURLを設定してください。",
            )
            return
        try:
            # 通信が詰まっても画面を固めない（つながらない PC では最長 10 秒待つ）
            info = self._pump("更新を確認しています…",
                              lambda: updater.check(url, include_skipped=True))
        except _Cancelled:
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "更新の確認", f"確認できませんでした:\n{exc}")
            return
        if not info:
            QMessageBox.information(self, "更新の確認", "お使いのバージョンは最新です。")
            return

        if info.get("skipped"):
            ret = QMessageBox.question(
                self, "更新があります",
                f"新しいバージョン {info['version']} は、以前この PC で起動の確認が"
                "できなかったため見送っています。\nもう一度試しますか？")
            if ret != QMessageBox.StandardButton.Yes:
                return
        notes = info.get("notes", "")
        ret = QMessageBox.question(
            self, "更新があります",
            f"新しいバージョン {info['version']} があります。\n\n{notes}\n\n"
            "今すぐ更新しますか？（ダウンロード後、アプリを再起動します）",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        dlg = QProgressDialog("ダウンロード中…", "キャンセル", 0, 100, self)
        dlg.setWindowTitle("更新")
        dlg.setMinimumDuration(0)

        def progress(done, total):
            if total > 0:
                dlg.setValue(int(done * 100 / total))
            QApplication.processEvents()
            return not dlg.wasCanceled()

        try:
            zip_path = updater.download(info["url"], progress=progress)
        except Exception as exc:  # noqa: BLE001
            dlg.close()
            QMessageBox.warning(self, "更新", f"ダウンロードに失敗しました:\n{exc}")
            return
        dlg.close()
        # 改ざん検証（update.json に sha256 があれば照合）
        if not updater.verify(zip_path, info.get("sha256", "")):
            QMessageBox.critical(
                self, "更新",
                "ダウンロードしたファイルの検証(SHA256)に失敗しました。\n"
                "更新を中止します。",
            )
            return
        # 入れ替える前に、新しい版がこの PC で起動できるかを試す（壊れた版を
        # 入れてしまうと、起動できず自動更新でも直せなくなる）
        wait = QProgressDialog("新しい版を展開しています…", None, 0, 0, self)
        wait.setWindowTitle("更新")
        wait.setMinimumDuration(0)
        wait.show()
        QApplication.processEvents()
        try:
            # 展開は 300MB ほどを書くので、画面を固めないよう別スレッドで
            src_dir = self._pump(None, lambda: updater.extract(zip_path), dialog=wait)
            wait.setLabelText("新しい版がこの PC で動くかを確かめています…")
            ok, detail = updater.trial_run(src_dir, poll=QApplication.processEvents)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        wait.close()
        if not ok:
            from . import errorlog, rollback
            rollback.skip_version(info["version"])
            try:
                errorlog.write(detail, context=f"更新の確認で失敗（{info['version']}）")
            except Exception:  # noqa: BLE001
                pass
            last = (detail.strip().splitlines() or [""])[-1][:300]
            QMessageBox.warning(
                self, "更新を見送りました",
                f"新しいバージョン {info['version']} は、この PC で起動の確認ができなかった"
                "ため、更新を見送りました。今の版のままお使いいただけます。\n\n"
                f"（詳しい内容: {last}）")
            return
        self._apply_update_and_quit(zip_path, src_dir)

    def _apply_update_and_quit(self, zip_path: str, src_dir: str) -> None:
        """アプリを本当に終了して、更新を入れ替える。

        QApplication.quit() だけでは終わらない: 終了は closeEvent を通るので、タブが
        開いていると「ウィンドウを閉じます。どうしますか？」が出る（既定の選択は
        キャンセル）し、未保存の変更の確認でもキャンセルできる。取りやめると終了しない
        のに、入れ替え役はアプリが終わるのを待つだけで、60 秒で待ちきれて、動いている
        版の上に新しい版を書いてしまっていた（実行中の exe は書き換えられず、新旧が
        混ざる）。そのため、先に本当に終了させ（未保存の変更は聞く）、終わると決まって
        から入れ替え役を起動する。
        """
        from . import errorlog, updater

        def _apply() -> None:
            try:
                updater.apply_and_restart(zip_path, extracted=src_dir)
            except Exception as exc:  # noqa: BLE001  終了のさなかなので、記録だけ残す
                try:
                    errorlog.write(str(exc), context="更新の適用に失敗")
                except Exception:  # noqa: BLE001
                    pass

        # quit() は aboutToQuit をその場で（closeEvent の中で）出すので、close() の前につなぐ
        app = QApplication.instance()
        app.aboutToQuit.connect(_apply)
        self._force_quit = True   # 確認とトレイへの退避を飛ばす（未保存の変更は聞く）
        if not self.close():      # 未保存の確認で取りやめられた
            self._force_quit = False
            app.aboutToQuit.disconnect(_apply)
            QMessageBox.information(
                self, "更新",
                "更新を取りやめました。保存していない変更が残っているため、"
                "アプリを終了していません。\n保存してから、もう一度更新してください。")
