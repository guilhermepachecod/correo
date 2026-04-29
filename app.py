from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
from database import db, Account, Email, Attachment
from mail_service import MailService
import threading, io

app = Flask(__name__)
app.config["SECRET_KEY"] = "x"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////app/database/mail.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins="*")
mail = MailService(db, socketio)

with app.app_context():
    db.create_all()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/accounts")
def accounts():
    return jsonify([a.to_dict() for a in Account.query.all()])


@app.route("/api/emails")
def emails():
    q = Email.query.order_by(Email.date.desc())
    return jsonify([e.to_list() for e in q.limit(200).all()])


@app.route("/api/email/<int:id>")
def email(id):
    e = Email.query.get_or_404(id)
    e.is_read = True
    db.session.commit()
    return jsonify(e.to_dict())


@app.route("/api/sync/<int:id>", methods=["POST"])
def sync(id):
    threading.Thread(target=lambda: mail.sync_account(id)).start()
    return jsonify({"ok": True})


@app.route("/api/send", methods=["POST"])
def send():
    return jsonify(mail.send_email(**request.json))


@app.route("/api/attachment/<int:id>")
def attachment(id):
    a = Attachment.query.get_or_404(id)
    return send_file(io.BytesIO(a.data), download_name=a.filename, as_attachment=True)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=15002)