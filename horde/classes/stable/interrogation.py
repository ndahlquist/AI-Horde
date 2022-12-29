import uuid
import enum

from datetime import datetime
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy import Enum, JSON, func, or_

from horde.logger import logger
from horde.flask import db, SQLITE_MODE
from horde.vars import thing_divisor
from horde.utils import get_expiry_date, get_interrogation_form_expiry_date


uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)
json_column_type = JSONB if not SQLITE_MODE else JSON

class State(enum.Enum):
    WAITING = 0
    PROCESSING = 1
    DONE = 2
    CANCELLED = 3
    FAULTED = 4



class InterrogationsForms(db.Model):
    """For storing the details of each image interrogation form"""
    __tablename__ = "interrogation_forms"
    id = db.Column(db.Integer, primary_key=True)
    i_id = db.Column(uuid_column_type(), db.ForeignKey("interrogations.id", ondelete="CASCADE"), nullable=False)
    interrogation = db.relationship(f"Interrogation", back_populates="forms")
    name = db.Column(db.String(30), nullable=False)
    state = db.Column(Enum(State), default=0, nullable=False) 
    payload = db.Column(json_column_type, default=None)
    result = db.Column(json_column_type, default=None)
    worker_id = db.Column(db.Integer, db.ForeignKey("workers.id"))
    worker = db.relationship("WorkerExtended", back_populates="interrogation_forms")
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    initiated =  db.Column(db.DateTime, default=None, index=True)
    expiry = db.Column(db.DateTime, default=None, index=True)

    def pop(self, worker):
        myself_refresh = db.session.query(
            InterrogationsForms
        ).filter(
            InterrogationsForms.id == self.id, 
            InterrogationsForms.state == State.WAITING
        ).with_for_update().first()
        if not myself_refresh:
            return None
        myself_refresh.state = State.PROCESSING
        db.session.commit()
        self.expiry = get_interrogation_form_expiry_date()
        self.initiated = datetime.utcnow()
        self.worker_id = worker.id
        db.session.commit()
        return {
            "name": self.name,
            "payload": self.payload,
        }
    
    def deliver(self, result):
        if self.state != State.PROCESSING:
            return(0)
        self.result = result
        # Each interrogation rewards 1 kudos
        self.state = State.DONE
        kudos = 1
        self.record(things_per_sec, kudos)
        db.session.commit()
        return(kudos)

    def cancel(self):
        if self.state != State.PROCESSING:
            return(0)
        self.result = None
        # Each interrogation rewards 1 kudos
        self.state = State.CANCELLED
        kudos = 1
        self.record(things_per_sec, kudos)
        db.session.commit()
        return(kudos)

    def record(self, kudos):
        cancel_txt = ""
        if self.state == State.CANCELLED:
            cancel_txt = " CANCELLED"
        self.worker.record_interrogation(kudos = kudos, seconds_taken = (datetime.utcnow() - self.initiated).seconds)
        self.interrogation.record_usage(raw_things = self.wp.things, kudos = kudos)
        logger.info(f"New{cancel_txt} Form {self.id} ({self.name}) worth {kudos} kudos, delivered by worker: {self.worker.name} for wp {self.interrogation.id}")


    def abort(self):
        '''Called when this request needs to be stopped without rewarding kudos. Say because it timed out due to a worker crash'''
        if self.state != State.PROCESSING:
            return        
        self.state = State.FAULTED
        self.worker.log_aborted_job()
        self.log_aborted_interrogation()
        # We return it to WAITING to let another worker pick it up
        self.state = State.WAITING
        db.session.commit()
        
    def log_aborted_interrogation(self):
        logger.info(f"Aborted Stale Interrogation {self.id} ({self.name}) from by worker: {self.worker.name} ({self.worker.id})")

    def is_completed(self):
        return self.state == State.DONE

    def is_FAULTED(self):
        return self.state == State.FAULTED

    def is_stale(self, ttl):
        if self.state in [State.FAULTED, State.CANCELLED, State.DONE]:
            return False
        return datetime.utcnow() > self.expiry

    def delete(self):
        db.session.delete(self)
        db.session.commit()


class Interrogation(db.Model):
    """For storing the request for interrogating an image"""
    __tablename__ = "interrogations"
    id = db.Column(uuid_column_type(), primary_key=True, default=uuid.uuid4) 
    source_image = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="interrogations")
    ipaddr = db.Column(db.String(39))  # ipv6
    safe_ip = db.Column(db.Boolean, default=False, nullable=False)
    trusted_workers = db.Column(db.Boolean, default=False, nullable=False)
    r2stored = db.Column(db.Boolean, default=False, nullable=False)
    expiry = db.Column(db.DateTime, default=get_expiry_date, index=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    forms = db.relationship("InterrogationsForms", back_populates="interrogation", cascade="all, delete-orphan")


    def __init__(self, forms, *args, **kwargs):
        super().__init__(*args, **kwargs)
        db.session.add(self)
        db.session.commit()
        self.set_forms(forms)


    def set_source_image(self, source_image):
        self.source_image = source_image
        db.session.commit()

    def refresh(self):
        self.expiry = get_expiry_date()
        db.session.commit()

    def is_stale(self):
        if datetime.utcnow() > self.expiry:
            return(True)
        return(False)

    def set_forms(self, forms = None):
        if not forms: forms = []
        seen_names = []
        for form in forms:
            # We don't allow the same interrogation type twice
            if form["name"] in seen_names:
                continue
            form_entry = InterrogationsForms(
                name=form["name"],
                payload=form.get("payload"),
                i_id=self.id
            )
            db.session.add(form_entry)

    def get_form_names(self):
        return [f.name for f in self.forms]

    def start_interrogation(self, worker):
        # We have to do this to lock the row for updates, to ensure we don't have racing conditions on who is picking up requests
        myself_refresh = db.session.query(Interrogation).filter(Interrogation.id == self.id, Interrogation.n > 0).with_for_update().first()
        if not myself_refresh:
            return None
        myself_refresh.n -= 1
        db.session.commit()
        worker_id = worker.id
        self.refresh()
        logger.audit(f"Interrogation with ID {self.id} popped by worker {worker.id} ('{worker.name}' / {worker.ipaddr})")
        return self.get_pop_payload()


    def get_pop_payload(self):
        interrogation_payload = {
            "id": self.id,
            "source_image": self.source_image,
            "forms": self.get_form_names(),
        }
        return(interrogation_payload)

    def needs_interrogation(self):
        return any(form.result == None for form in self.form)

    def is_completed(self):
        if self.FAULTED:
            return True
        if self.needs_interrogation():
            return False
        return True


    def get_status(
            self, 
        ):
        ret_dict = {
            "state": State.WAITING,
            "forms": [],
        }
        all_faulted = True
        all_done = True
        processing = False
        for form in self.forms:
            form_dict = {
                "name": form.name,
                "state": form.state.name,
                "result": form.result,
            }
            ret_dict["forms"].append(form_dict)
            if form.state != State.FAULTED:
                all_faulted = False
            if form.state != State.DONE:
                all_done = False
            if form.state == State.PROCESSING:
                processing = True
        if all_faulted:
            ret_dict["state"] = State.FAULTED.name
        elif all_done:
            ret_dict["state"] = State.DONE.name
        elif processing:
            ret_dict["state"] = State.PROCESSING.name
        return(ret_dict)

    def record_usage(self, kudos):
        '''Record that we received a requested interrogation and how much kudos it costs us
        '''
        self.user.record_usage(0, kudos)
        self.refresh()