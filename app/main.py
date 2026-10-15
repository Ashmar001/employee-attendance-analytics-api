

import os

import certifi

import re

import calendar

import json

from bson import json_util

from datetime import datetime, timedelta, timezone, date

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional, Literal

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query

from pydantic import BaseModel, Field, field_validator, model_validator

from pymongo import MongoClient, ReturnDocument, ASCENDING, DESCENDING

from pymongo.errors import DuplicateKeyError, OperationFailure

from bson import json_util

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=30000,
)

db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")

STATUSES = (*PRESENCE, "ABSENT", "LEAVE")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def epoch_ms(value: datetime) -> int:

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    return int(value.timestamp() * 1000)

def api_doc(doc: dict) -> dict:

    """Serialize Mongo datetimes to epoch milliseconds; never expose _id."""

    result = dict(doc)

    result.pop("_id", None)

    for key in ("created_at", "punch_in", "punch_out", "at"):

        if isinstance(result.get(key), datetime):

            result[key] = epoch_ms(result[key])

    history = result.get("history") or []

    converted = []

    for entry in history:

        e = dict(entry)

        if isinstance(e.get("at"), datetime):

            e["at"] = epoch_ms(e["at"])

        changes = e.get("changes") or {}

        for change in changes.values():

            if isinstance(change, dict):

                for k in ("from", "to"):

                    if isinstance(change.get(k), datetime):

                        change[k] = epoch_ms(change[k])

        converted.append(e)

    if "history" in result:

        result["history"] = converted

    return result

def parse_ms(value: Optional[int], *, required: bool = False) -> Optional[datetime]:

    if value is None:

        if required:

            raise HTTPException(422, "Timestamp is required")

        return None

    if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000 or value > 253_402_300_799_999:

        raise HTTPException(422, "Timestamps must be epoch milliseconds")

    try:

        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)

    except (OverflowError, OSError, ValueError):

        raise HTTPException(422, "Timestamp is out of range")

def parse_date(value: str) -> date:

    try:

        parsed = date.fromisoformat(value)

        if parsed.isoformat() != value:

            raise ValueError

        return parsed

    except (ValueError, TypeError):

        raise HTTPException(422, "Date must use YYYY-MM-DD")

def parse_month(month: str) -> tuple[date, date]:

    if not re.fullmatch(r"\d{4}-\d{2}", month):

        raise HTTPException(422, "month must use YYYY-MM")

    try:

        y, m = map(int, month.split("-"))

        start = date(y, m, 1)

        end = date(y, m, calendar.monthrange(y, m)[1])

        return start, end

    except ValueError:

        raise HTTPException(422, "month must use YYYY-MM")

def round_half_up(value: float, places: int = 2) -> float:

    quantum = Decimal("1") if places == 0 else Decimal("0." + "0" * (places - 1) + "1")

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def working_days(start: date, end: date) -> int:

    if end < start:

        return 0

    days = (end - start).days + 1

    full_weeks, extra = divmod(days, 7)

    count = full_weeks * 5

    for i in range(extra):

        if (start.weekday() + i) % 7 < 5:

            count += 1

    return count

def shift_start_dt(attendance_date: str, shift_start: str) -> datetime:

    day = parse_date(attendance_date)

    h, m = map(int, shift_start.split(":"))

    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)

def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:

    local = punch_in.astimezone(IST)

    start = shift_start_dt(attendance_date, shift_start)

    elapsed = (local - start).total_seconds()

    return int(elapsed // 60) if elapsed > 600 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:

    seconds = (punch_out - punch_in).total_seconds()

    return float((Decimal(str(seconds)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:

    day = parse_date(attendance_date)

    sh, sm = map(int, shift_start.split(":"))

    eh, em = map(int, shift_end.split(":"))

    if (eh, em) <= (sh, sm):

        day += timedelta(days=1)

    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST).astimezone(UTC)

    minutes = int((punch_out.astimezone(UTC) - end).total_seconds() // 60)

    return minutes if minutes >= 30 else 0

def attendance_day(punch: datetime, employee: dict) -> str:

    local = punch.astimezone(IST)

    sh, sm = map(int, employee["shift_start"].split(":"))

    eh, em = map(int, employee["shift_end"].split(":"))

    d = local.date()

    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):

        d -= timedelta(days=1)

    return d.isoformat()

def validate_clock(value: str) -> str:

    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):

        raise ValueError("Time must use HH:MM in 24-hour format")

    return value

class EmployeeIn(BaseModel):

    emp_code: str = Field(min_length=1, max_length=40)

    name: str = Field(min_length=1, max_length=120)

    email: str = Field(min_length=3, max_length=254)

    department: str = Field(min_length=1, max_length=100)

    shift_start: str = "09:30"

    shift_end: str = "18:30"

    joined_on: str

    @field_validator("shift_start", "shift_end")

    @classmethod

    def valid_clock(cls, value):

        return validate_clock(value)

    @field_validator("joined_on")

    @classmethod

    def valid_join_date(cls, value):

        parse_date(value)

        return value

    @field_validator("email")

    @classmethod

    def valid_email(cls, value):

        if "@" not in value or value.startswith("@") or value.endswith("@"):

            raise ValueError("Invalid email")

        return value

    @model_validator(mode="after")

    def shifts_must_differ(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self

class PunchInIn(BaseModel):

    emp_code: str

    punched_at: Optional[int] = None

    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class RegularizeRequest(BaseModel):

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None

    punch_in: Optional[int] = None

    punch_out: Optional[int] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

class PunchOutIn(BaseModel):

    emp_code: str

    punched_at: Optional[int] = None

@app.on_event("startup")

def create_indexes():

    def ensure_index(collection, keys, name, unique=False):

        expected_keys = list(keys)

        existing = collection.index_information()

        for index_name, info in existing.items():

            actual_keys = [(key, direction) for key, direction in info["key"]]

            if actual_keys == expected_keys:

                existing_unique = info.get("unique", False)

                if existing_unique != unique:

                    raise RuntimeError(

                        f"Index {index_name} has conflicting unique settings"

                    )

                print(f"Index already exists: {index_name}")

                return

        collection.create_index(

            expected_keys,

            unique=unique,

            name=name

        )

        print(f"Created index: {name}")

    ensure_index(

        db.employees,

        [("emp_code", ASCENDING)],

        "uq_employee_code",

        unique=True

    )

    ensure_index(

        db.employees,

        [("department", ASCENDING), ("emp_code", ASCENDING)],

        "ix_employee_department_code"

    )

    ensure_index(

        db.attendance_logs,

        [("emp_code", ASCENDING), ("date", ASCENDING)],

        "uq_attendance_employee_date",

        unique=True

    )

    ensure_index(

        db.attendance_logs,

        [("date", DESCENDING), ("emp_code", ASCENDING)],

        "ix_attendance_date_code"

    )

    ensure_index(

        db.attendance_logs,

        [("emp_code", ASCENDING), ("date", ASCENDING),

         ("status", ASCENDING)],

        "ix_attendance_filters"

    )

    ensure_index(

        db.attendance_logs,

        [("emp_code", ASCENDING), ("punch_out", ASCENDING),

         ("punch_in", DESCENDING)],

        "ix_open_punch"

    )

@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        return {"status": "ok"}

    except Exception:

        raise HTTPException(status_code=503, detail="Database unavailable")

@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(status_code=409, detail="emp_code already exists")

    return api_doc(doc)

@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    q = {"department": department} if department is not None else {}

    total = db.employees.count_documents(q)

    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))

    for item in items:

        if isinstance(item.get("created_at"), datetime):

            item["created_at"] = epoch_ms(item["created_at"])

    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(status_code=404, detail="Employee not found")

    ts = parse_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)

    d = attendance_day(ts, emp)

    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None,

           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),

           "overtime_minutes": 0, "half_day": False, "history": []}

    try:

        db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(status_code=409, detail="already punched in for this date")

    return api_doc(doc)

@app.get("/attendance")

def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,

                   status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):

    if date_from:

        parse_date(date_from)

    if date_to:

        parse_date(date_to)

    if date_from and date_to and date_from > date_to:

        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    if status is not None and status not in STATUSES:

        raise HTTPException(status_code=422, detail="Invalid status")

    q = {}

    if emp_code is not None: q["emp_code"] = emp_code

    if status is not None: q["status"] = status

    if date_from or date_to:

        q["date"] = {}

        if date_from: q["date"]["$gte"] = date_from

        if date_to: q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)

    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))

    return {"items": [api_doc(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(status_code=404, detail="Employee not found")

    out = parse_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)

    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out}}, sort=[("punch_in", DESCENDING)])

    if not record:

        raise HTTPException(status_code=404, detail="No punch-in record found")

    if record.get("punch_out") is not None:

        raise HTTPException(status_code=409, detail="Attendance record was already punched out")

    pin = record["punch_in"]

    if pin.tzinfo is None: pin = pin.replace(tzinfo=UTC)

    seconds = (out - pin).total_seconds()

    if seconds <= 0 or seconds > 86400:

        raise HTTPException(status_code=422, detail="Punch-out must be after punch-in and within 24 hours")

    hours = compute_work_hours(pin, out)

    overtime = compute_overtime(out, emp["shift_end"], record["date"], emp["shift_start"])

    updated = db.attendance_logs.find_one_and_update({"emp_code": body.emp_code, "date": record["date"], "punch_out": None},

        {"$set": {"punch_out": out, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}}, return_document=ReturnDocument.AFTER)

    if not updated:

        raise HTTPException(status_code=409, detail="Attendance record was already punched out")

    return api_doc(updated)

@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):

    parse_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee: raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})

    if not record: raise HTTPException(status_code=404, detail="Attendance record not found")

    changes = {}

    status = body.status if body.status is not None else record.get("status", "PRESENT")

    pin = parse_ms(body.punch_in) if body.punch_in is not None else record.get("punch_in")

    pout = parse_ms(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    if status in ("ABSENT", "LEAVE"):

        if body.punch_in is not None or body.punch_out is not None:

            raise HTTPException(status_code=422, detail="ABSENT and LEAVE cannot have punch times")

        pin = pout = None

    elif pin is None:

        raise HTTPException(status_code=422, detail="Presence status requires punch_in")

    if pin is not None and attendance_day(pin, employee) != date:

        raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")

    if pout is not None:

        if pin is None or (pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 86400:

            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    desired = {"status": status, "punch_in": pin, "punch_out": pout}

    old = {k: record.get(k) for k in desired}

    for key, val in desired.items():

        if old.get(key) != val:

            changes[key] = {"from": old.get(key), "to": val}

    if not changes:

        raise HTTPException(status_code=422, detail="Request does not change the record")

    derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}

    if pin is not None:

        derived["late_minutes"] = compute_late_minutes(pin, employee["shift_start"], date)

    if pin is not None and pout is not None:

        derived["work_hours"] = compute_work_hours(pin, pout)

        derived["overtime_minutes"] = compute_overtime(pout, employee["shift_end"], date, employee["shift_start"])

        derived["half_day"] = derived["work_hours"] < 4.5

    for key, val in derived.items():

        if record.get(key) != val:

            changes[key] = {"from": record.get(key), "to": val}

    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    updated = db.attendance_logs.find_one_and_update({"emp_code": emp_code, "date": date, "history": record.get("history", [])},

        {"$set": {**desired, **derived}, "$push": {"history": history_entry}}, return_document=ReturnDocument.AFTER)

    if not updated: raise HTTPException(status_code=409, detail="Record changed concurrently; retry")

    return api_doc(updated)

def month_bounds(month: str):

    start, end = parse_month(month)

    return start.isoformat(), end.isoformat(), start, end

def present_fraction_expr():

    return {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str):

    start_s, end_s, start, end = month_bounds(month)

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp: raise HTTPException(status_code=404, detail="Employee not found")

    work_start = max(start, parse_date(emp["joined_on"]))

    wd = working_days(work_start, end) if work_start <= end else 0

    pipeline = [

        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},

        {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}

    ]

    row = next(db.attendance_logs.aggregate(pipeline), {})

    present = round_half_up(row.get("present_days", 0))

    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present, "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0), "total_late_minutes": row.get("total_late_minutes", 0), "overtime_minutes": row.get("overtime_minutes", 0), "attendance_pct": round_half_up(present / wd * 100, 2) if wd else None}

@app.get("/analytics/departments/summary")

def department_summary(month: str, department: Optional[str] = None):

    start_s, end_s, start, end = month_bounds(month)

    employee_filter = {"joined_on": {"$lte": end_s}}

    if department is not None: employee_filter["department"] = department

    pipeline = [

        {"$match": employee_filter},

        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}], "as": "logs"}},

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},

                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start_s]}}}}, 6]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start_s]}}}}, 2]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},

                    "hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},

                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},

                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},

        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": {"$round": ["$present_days", 2]}, "avg_work_hours": {"$cond": [{"$eq": ["$hours", None]}, None, {"$round": ["$hours", 2]}]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},

        {"$sort": {"department": 1}}

    ]

    return {"month": month, "items": list(db.employees.aggregate(pipeline))}

@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):

    start_s, end_s, _, _ = month_bounds(month)

    pipeline = [

        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},

        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"},

    ]

    if department is not None: pipeline.append({"$match": {"employee.department": department}})

    pipeline += [

        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},

        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": limit}}},

        {"$project": {"_id": 0, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "rank": 1}},

        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}

    ]

    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):

    # Keep the public query parameter names exactly as defined in openapi.yaml.

    if not from_date or not to_date:

        raise HTTPException(status_code=422, detail="from and to are required")

    start, end = parse_date(from_date), parse_date(to_date)

    if end < start or (end - start).days + 1 > 92:

        raise HTTPException(status_code=422, detail="Invalid date range; maximum 92 days")

    if not db.employees.find_one({"department": department}):

        raise HTTPException(status_code=404, detail="Unknown department")

    # Gap-filled date series is generated in MongoDB; headcount and rate are derived per date.

    pipeline = [

        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},

        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},

        {"$unwind": "$employee"}, {"$match": {"employee.department": department}},

        {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},

        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},

        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [datetime(start.year,start.month,start.day), datetime(end.year,end.month,end.day)]}}},

        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},

        {"$sort": {"date": 1}}

    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Add headcount/rate to the database-produced date rows; rows remain gap-filled by Mongo.

    for row in rows:

        day = parse_date(row["date"])

        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": row["date"]}})

        working = day.weekday() < 5

        row["is_working_day"] = working

        row["headcount"] = headcount

        row["attendance_rate"] = round_half_up(row["present_count"] / headcount, 4) if working and headcount else None

    for i, row in enumerate(rows):

        rates = [r["attendance_rate"] for r in rows[max(0, i-6):i+1] if r["attendance_rate"] is not None]

        row["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4) if rates else None

    return {"department": department, "from": from_date, "to": to_date, "items": rows}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50),
):
    def json_safe(result):
        return json.loads(json_util.dumps(result))

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if status:
            query["status"] = status

        start_date = date_from or from_date
        end_date = date_to or to_date

        if start_date or end_date:
            query["date"] = {}
            if start_date:
                query["date"]["$gte"] = start_date
            if end_date:
                query["date"]["$lte"] = end_date

        result = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return json_safe(result)

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        start, end, _, _ = month_bounds(month)

        result = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "emp_code": emp_code,
                            "date": {"$gte": start, "$lte": end},
                        }
                    },
                    {"$group": {"_id": None, "count": {"$sum": 1}}},
                ],
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return json_safe(result)

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        _, end, _, _ = month_bounds(month)
        match = {"joined_on": {"$lte": end}}

        if department:
            match["department"] = department

        result = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": [
                    {"$match": match},
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "logs",
                        }
                    },
                ],
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return json_safe(result)

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        start, end, _, _ = month_bounds(month)

        result = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start, "$lte": end},
                            "late_minutes": {"$gt": 0},
                        }
                    },
                    {
                        "$group": {
                            "_id": "$emp_code",
                            "total": {"$sum": "$late_minutes"},
                        }
                    },
                ],
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return json_safe(result)

    elif endpoint == "department_trend":
        start_date = date_from or from_date
        end_date = date_to or to_date

        if not department or not start_date or not end_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )

        parse_date(start_date)
        parse_date(end_date)

        if start_date > end_date:
            raise HTTPException(
                status_code=422,
                detail="from date must not be after to date",
            )

        result = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": start_date,
                                "$lte": end_date,
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                ],
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return json_safe(result)

    else:
        raise HTTPException(
            status_code=422,
            detail="Unknown endpoint name",
        )

# --------------------------------------------------------------------------- #

# TODO - the rest of the contract (see openapi.yaml):

#   POST  /attendance/punch-out

#   PATCH /attendance/{emp_code}/{date}

#   GET   /analytics/employees/{emp_code}/monthly

#   GET   /analytics/departments/summary

#   GET   /analytics/leaderboard/late

#   GET   /analytics/departments/{department}/trend

#   GET   /admin/explain/{endpoint}

# --------------------------------------------------------------------------- #