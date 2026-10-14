import os
import re
import json
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from bson import json_util
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

@asynccontextmanager
async def lifespan(_app):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

def create_indexes():
    """Create indexes safely when the application starts."""
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp",
    )

    logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING),
        ],
        name="attendance_employee_date_status",
    )

    logs.create_index(
        [
            ("date", ASCENDING),
            ("late_minutes", DESCENDING),
            ("emp_code", ASCENDING),
        ],
        name="attendance_late_date",
    )

    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="employee_department_joined",
    )

    employees.create_index(
        [("joined_on", ASCENDING)],
        name="employee_joined",
    )

    logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_emp_punchin",
    )


def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        fail422(f"{field} must be YYYY-MM-DD")


def validate_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        fail422("month must be YYYY-MM")

    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])

    return first, last


def validate_epoch(value):
    """
    Validate epoch milliseconds and truncate to whole seconds.

    Values that look like epoch seconds are rejected.
    """
    if (
        type(value) is not int
        or value < 100_000_000_000
        or value > 4_102_444_800_000
    ):
        fail422(
            "timestamp must be epoch milliseconds between "
            "100000000000 and 4102444800000"
        )

    return datetime.fromtimestamp(value // 1000, UTC).replace(
        microsecond=0
    )


def as_utc(dt: datetime) -> datetime:
    """PyMongo may return naive UTC datetimes."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def epoch_ms(value):
    if value is None:
        return None
    return int(as_utc(value).timestamp()) * 1000


def round_half_up(value, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def time_parts(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(
        r"([01]\d|2[0-3]):[0-5]\d", value
    ):
        fail422("time must be HH:MM in 24-hour format")

    h, m = value.split(":")
    return int(h), int(m)


def shift_start_instant(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
    record_date: str,
) -> datetime:
    """
    Calculate the shift start in IST.

    For an overnight shift, an early-morning punch-in before the shift
    end belongs to the previous calendar day's shift.
    """
    sh, sm = time_parts(shift_start)
    base = date.fromisoformat(record_date)

    return datetime.combine(base, time(sh, sm), IST)


def shift_end_instant(
    shift_start_dt: datetime,
    shift_start: str,
    shift_end: str,
) -> datetime:
    sh, sm = time_parts(shift_start)
    eh, em = time_parts(shift_end)

    end_day = shift_start_dt.date()

    if (eh, em) <= (sh, sm):
        end_day += timedelta(days=1)

    return datetime.combine(end_day, time(eh, em), IST)


def attendance_date_for_punch(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """Return the attendance date associated with a punch-in."""
    local_in = as_utc(punch_in).astimezone(IST)

    sh, sm = time_parts(shift_start)
    eh, em = time_parts(shift_end)

    record_day = local_in.date()

    if (
        (eh, em) <= (sh, sm)
        and (local_in.hour, local_in.minute) < (eh, em)
    ):
        record_day -= timedelta(days=1)

    return record_day.isoformat()


def derived_fields(
    emp: dict,
    rec_date: str,
    pin: Optional[datetime],
    pout: Optional[datetime],
):
    """Calculate derived attendance fields."""
    late = 0
    work = None
    overtime = 0
    half = False

    if pin is not None:
        start = shift_start_instant(
            pin,
            emp["shift_start"],
            emp["shift_end"],
            rec_date,
        )

        elapsed = int(
            (as_utc(pin) - start.astimezone(UTC)).total_seconds()
        )

        if elapsed > 600:
            late = elapsed // 60

    if pin is not None and pout is not None:
        seconds = int(
            (as_utc(pout) - as_utc(pin)).total_seconds()
        )

        hours = Decimal(seconds) / Decimal(3600)
        rounded_hours = hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        work = float(rounded_hours)
        half = rounded_hours < Decimal("4.50")

        start = shift_start_instant(
            pin,
            emp["shift_start"],
            emp["shift_end"],
            rec_date,
        )

        end = shift_end_instant(
            start,
            emp["shift_start"],
            emp["shift_end"],
        )

        extra = int(
            (as_utc(pout) - end.astimezone(UTC)).total_seconds()
        )

        overtime = extra // 60 if extra >= 1800 else 0

    return {
        "work_hours": work,
        "late_minutes": int(late),
        "overtime_minutes": int(overtime),
        "half_day": bool(half),
    }


def api_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}

    if "created_at" in out:
        out["created_at"] = epoch_ms(out["created_at"])

    return out


def api_log(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}

    out.setdefault("work_hours", None)
    out["late_minutes"] = out.get("late_minutes") or 0
    out["overtime_minutes"] = out.get("overtime_minutes") or 0
    out["half_day"] = bool(out.get("half_day", False))

    for key in ("punch_in", "punch_out"):
        out[key] = epoch_ms(out.get(key))

    history = []

    for item in out.get("history", []):
        entry = dict(item)
        entry["at"] = epoch_ms(entry.get("at"))

        changes = {}

        for field, change in entry.get("changes", {}).items():
            pair = dict(change)

            if field in ("punch_in", "punch_out"):
                pair["from"] = epoch_ms(pair.get("from"))
                pair["to"] = epoch_ms(pair.get("to"))

            changes[field] = pair

        entry["changes"] = changes
        history.append(entry)

    out["history"] = history
    return out

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def valid_shift(self):
        if self.shift_start == self.shift_end:
            fail422("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            validate_epoch(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            validate_epoch(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            validate_epoch(v)
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return api_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}

    total = employees.count_documents(q)

    cursor = (
        employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [api_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    ts = (
        validate_epoch(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )

    day = attendance_date_for_punch(
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    doc.update(derived_fields(emp, day, ts, None))

    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "already punched in for this date",
        )

    return api_log(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    ts = (
        validate_epoch(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )

    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )

    if rec is None:
        raise HTTPException(404, "no punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")

    elapsed = (ts - as_utc(rec["punch_in"])).total_seconds()

    if elapsed <= 0 or elapsed > 24 * 3600:
        fail422("punched_at must be after punch_in and within 24 hours")

    derived = derived_fields(emp, rec["date"], rec["punch_in"], ts)

    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "record is already punched out")

    return api_log(updated)

# Attendance List
def attendance_filter(
    emp_code=None,
    date_from=None,
    date_to=None,
    status=None,
):
    q = {}

    if emp_code is not None:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status is not None:
        q["status"] = status

    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")

    if date_to:
        parse_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        fail422("date_from must be on or before date_to")

    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)

    cursor = (
        logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [api_log(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }

# Regularization
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)

    emp = employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    rec = logs.find_one({"emp_code": emp_code, "date": date})

    if not rec:
        raise HTTPException(404, "attendance record not found")

    original = {
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    requested = body.model_dump(exclude_unset=True)

    new_status = requested.get("status") or rec["status"]

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            requested.get("punch_in") is not None
            or requested.get("punch_out") is not None
        ):
            fail422("ABSENT and LEAVE cannot have punch times")

        pin = None
        pout = None

    else:
        pin = (
            validate_epoch(requested["punch_in"])
            if requested.get("punch_in") is not None
            else rec.get("punch_in")
        )

        pout = (
            validate_epoch(requested["punch_out"])
            if requested.get("punch_out") is not None
            else rec.get("punch_out")
        )

        if pin is None:
            fail422("presence status requires punch_in")

        expected_day = attendance_date_for_punch(
            pin,
            emp["shift_start"],
            emp["shift_end"],
        )

        if expected_day != date:
            fail422("punch_in does not belong to the attendance date")

        if pout is not None:
            delta = (as_utc(pout) - as_utc(pin)).total_seconds()

            if delta <= 0 or delta > 86400:
                fail422(
                    "punch_out must be after punch_in and within 24 hours"
                )

    derived = derived_fields(emp, date, pin, pout)

    final = {
        "status": new_status,
        "punch_in": pin,
        "punch_out": pout,
        **derived,
    }

    changes = {}

    for field, new_value in final.items():
        old_value = original.get(field)

        if field in ("punch_in", "punch_out"):
            old_cmp = epoch_ms(old_value)
            new_cmp = epoch_ms(new_value)

            if old_cmp != new_cmp:
                changes[field] = {
                    "from": old_value,
                    "to": new_value,
                }

        elif old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        fail422("request does not change the record")

    at = datetime.now(UTC).replace(microsecond=0)

    history_entry = {
        "at": at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filter_doc = {
        "_id": rec["_id"],
        "history": (
            rec["history"] if "history" in rec else {"$exists": False}
        ),
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }

    result = logs.update_one(
        filter_doc,
        {
            "$set": final,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "record changed concurrently; retry",
        )

    updated = logs.find_one({"_id": rec["_id"]})
    return api_log(updated)



def weekday_count(start: date, end: date) -> int:
    """Count weekdays inclusively."""
    count = 0
    cur = start

    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)

    return count


def month_match(first: date, last: date, emp_code=None):
    q = {
        "date": {
            "$gte": first.isoformat(),
            "$lte": last.isoformat(),
        }
    }

    if emp_code:
        q["emp_code"] = emp_code

    return q


def monthly_pipeline(first: date, last: date, emp_code: str):
    return [
        {
            "$match": month_match(first, last, emp_code)
        },
        {
            "$addFields": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date"
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {"$in": ["$_weekday", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    emp = employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(404, "employee not found")

    first, last = validate_month(month)
    joined = date.fromisoformat(emp["joined_on"])

    working = (
        weekday_count(max(first, joined), last)
        if joined <= last
        else 0
    )

    pipeline = monthly_pipeline(first, last, emp_code)

    rows = list(logs.aggregate(pipeline))
    row = rows[0] if rows else {}

    present = float(row.get("present_days", 0))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(
            row.get("total_overtime_minutes", 0)
        ),
        "attendance_pct": (
            round_half_up(present / working * 100, 2)
            if working
            else None
        ),
    }



def department_summary_pipeline(first, last, department=None):
    emp_match = {"joined_on": {"$lte": last.isoformat()}}

    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {
                                        "$gte": [
                                            "$date",
                                            first.isoformat(),
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            last.isoformat(),
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date"
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "records",
            }
        },
        {
            "$unwind": {
                "path": "$records",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$records.status",
                                            list(PRESENCE),
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$records._weekday",
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    "$records.half_day",
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$records.status",
                                            list(PRESENCE),
                                        ]
                                    },
                                    {
                                        "$ne": [
                                            "$records.work_hours",
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$records.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$records.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$records.late_minutes", 0]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$records.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$records.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$hours"}, 0]},
                        {"$avg": "$hours"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
):
    first, last = validate_month(month)

    pipeline = department_summary_pipeline(
        first,
        last,
        department,
    )

    items = list(employees.aggregate(pipeline))

    for item in items:
        item["present_days"] = round_half_up(
            item.get("present_days", 0),
            2,
        )

        if item.get("avg_work_hours") is not None:
            item["avg_work_hours"] = round_half_up(
                item["avg_work_hours"],
                2,
            )

    return {"month": month, "items": items}


# =========================================================
# LATE LEADERBOARD
# =========================================================

def leaderboard_pipeline(first, last, limit, department=None):
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append(
            {"$match": {"emp.department": department}}
        )

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "_id": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = validate_month(month)

    pipeline = leaderboard_pipeline(
        first,
        last,
        limit,
        department,
    )

    return {
        "month": month,
        "items": list(logs.aggregate(pipeline)),
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_value: str = Query(..., alias="from"),
    to_value: str = Query(..., alias="to"),
):
    return _department_trend(
        department,
        from_value,
        to_value,
    )


def half_up_4(expr):
    """Half-up rounding to 4 decimals inside a pipeline ($round rounds ties to even)."""
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expr, 10000]}, 0.5]}},
            10000,
        ]
    }


def trend_pipeline(department, start, end):
    """
    Build the trend aggregation.

    Starts with a MongoDB date spine so dates are still returned when
    the department has no attendance records for part or all of the range.
    """
    start_dt = datetime.combine(start, time.min, UTC)
    end_dt = datetime.combine(end, time.min, UTC)

    return [
        {
            "$documents": [
                {
                    "date": start_dt + timedelta(days=i)
                }
                for i in range((end - start).days + 1)
            ]
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                ],
                "as": "records",
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$records",
                            "as": "r",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$r.status", list(PRESENCE)]},
                                    {
                                        "$cond": [
                                            "$$r.half_day",
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$records",
                            "as": "r",
                            "cond": {
                                "$gt": ["$$r.late_minutes", 0]
                            },
                        }
                    }
                },
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                    }
                },
                "weekday": {"$dayOfWeek": "$date"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$day"]
                            },
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$hc.n", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$weekday", 2]},
                        {"$lte": ["$weekday", 6]},
                    ]
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        half_up_4(
                            {"$divide": ["$present_count", "$headcount"]}
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$type": "$moving_avg_7d"}, "null"]},
                        None,
                        half_up_4("$moving_avg_7d"),
                    ]
                },
            }
        },
    ]


def _department_trend(
    department: str,
    from_value: Optional[str],
    to_value: Optional[str],
):
    if not from_value or not to_value:
        fail422("from and to are required")

    start = parse_date(from_value, "from")
    end = parse_date(to_value, "to")

    if end < start:
        fail422("to must be on or after from")

    if (end - start).days + 1 > 92:
        fail422("date range cannot exceed 92 days")

    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "department not found")

    pipeline = trend_pipeline(department, start, end)

    # $documents must be the first stage and is supported through
    # database-level aggregation.
    items = list(db.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        fail422("unsupported endpoint")

    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")

        if date_to:
            parse_date(date_to, "date_to")

        if date_from and date_to and date_from > date_to:
            fail422("date_from must be on or before date_to")

        q = attendance_filter(
            emp_code,
            date_from,
            date_to,
            status,
        )

        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

        explain = db.command(
            "explain",
            command,
            verbosity="executionStats",
        )

        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            fail422("emp_code and month are required")

        first, last = validate_month(month)

        emp = employees.find_one({"emp_code": emp_code})

        if not emp:
            raise HTTPException(404, "employee not found")

        joined = date.fromisoformat(emp["joined_on"])
        pipeline = monthly_pipeline(first, last, emp_code)

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            fail422("month is required")

        first, last = validate_month(month)

        pipeline = department_summary_pipeline(
            first,
            last,
            department,
        )

        explain = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            fail422("month is required")

        first, last = validate_month(month)

        pipeline = leaderboard_pipeline(
            first,
            last,
            limit,
            department,
        )

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "attendance_logs"

    else:
        if not department or not from_date or not to_date:
            fail422("department, from and to are required")

        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")

        if end < start:
            fail422("to must be on or after from")

        if (end - start).days + 1 > 92:
            fail422("date range cannot exceed 92 days")

        if employees.count_documents({"department": department}) == 0:
            raise HTTPException(404, "department not found")

        pipeline = trend_pipeline(department, start, end)

        explain = db.command(
            "explain",
            {
                "aggregate": 1,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        collection = "database"

    explain = json.loads(json_util.dumps(explain, json_options=json_util.RELAXED_JSON_OPTIONS))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }
