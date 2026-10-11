"""Employee attendance API. Calendar values are IST strings; BSON datetimes are UTC instants."""
import os, re
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30)); UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
system_router = APIRouter(tags=["system"])
employee_router = APIRouter(prefix="/employees", tags=["employees"])
attendance_router = APIRouter(prefix="/attendance", tags=["attendance"])
analytics_router = APIRouter(prefix="/analytics", tags=["analytics"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

@app.on_event("startup")
def indexes():
    db.employees.create_index("emp_code", unique=True, name="employee_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="employee_department_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="employee_dept_joined")
    db.employees.create_index("joined_on", name="employee_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="attendance_employee_date_unique")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="attendance_monthly")

def as_utc(dt):
    if dt is None: return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
def epoch(dt):
    dt=as_utc(dt)
    return int(dt.timestamp()*1000) if dt else None
def parse_ms(v):
    if type(v) is not int or v < 100000000000 or v > 4102444800000:
        raise ValueError("timestamp must be epoch milliseconds in the supported range")
    return datetime.fromtimestamp(v/1000, UTC).replace(microsecond=0).replace(tzinfo=None)
def now_dt(): return datetime.now(UTC).replace(microsecond=0).replace(tzinfo=None)
def date_value(v):
    try: return date.fromisoformat(v).isoformat() if isinstance(v,str) and date.fromisoformat(v).isoformat()==v else (_ for _ in ()).throw(ValueError())
    except Exception: raise ValueError("date must be YYYY-MM-DD")
def safe_date(v):
    try: return date_value(v)
    except ValueError as e: raise HTTPException(422,str(e))
def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise ValueError("month must be YYYY-MM")
    y,m=map(int,month.split('-')); start=date(y,m,1); end=date(y+1,1,1) if m==12 else date(y,m+1,1)
    return start.isoformat(), (end-timedelta(days=1)).isoformat()
def shift_datetime(day, hm, next_day=False):
    d=date.fromisoformat(day)+(timedelta(days=1) if next_day else timedelta())
    h,m=map(int,hm.split(':')); return datetime(d.year,d.month,d.day,h,m,tzinfo=IST).astimezone(UTC).replace(tzinfo=None)
def attendance_day(ts, emp):
    local=as_utc(ts).astimezone(IST); day=local.date()
    if emp['shift_end'] <= emp['shift_start'] and local.strftime('%H:%M') < emp['shift_end']: day-=timedelta(days=1)
    return day.isoformat()
def late_value(ts, emp, day):
    start=shift_datetime(day,emp['shift_start']); elapsed=int((ts-start).total_seconds())
    return elapsed//60 if elapsed>600 else 0
def work_values(pin,pout,emp,day):
    seconds=int((pout-pin).total_seconds())
    hours=float((Decimal(seconds)/Decimal(3600)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    overnight=emp['shift_end']<=emp['shift_start']
    end=shift_datetime(day,emp['shift_end'],overnight)
    mins=int((pout-end).total_seconds())//60
    return hours, mins if mins>=30 else 0, hours<4.5
def api_record(d):
    out={k:v for k,v in d.items() if k!="_id"}
    out["punch_in"]=epoch(d.get("punch_in")); out["punch_out"]=epoch(d.get("punch_out"))
    out["history"]=[]
    for h in d.get("history",[]):
        item={"at":epoch(h.get("at")),"by":h.get("by"),"reason":h.get("reason"),"changes":{}}
        for k,ch in h.get("changes",{}).items(): item["changes"][k]={z:(epoch(v) if k in ("punch_in","punch_out") else v) for z,v in ch.items()}
        out["history"].append(item)
    out.setdefault("half_day",False); out.setdefault("late_minutes",0); out.setdefault("overtime_minutes",0); out.setdefault("work_hours",None)
    return out
def round2(v): return float(Decimal(str(v)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
def round4(v): return float(Decimal(str(v)).quantize(Decimal('.0001'),rounding=ROUND_HALF_UP))
def pg(page,size):
    if page<1 or not 1<=size<=100: raise HTTPException(422,"invalid pagination")

class EmployeeIn(BaseModel):
    model_config=ConfigDict(extra="ignore")
    emp_code:str=Field(pattern=r"^EMP\d{4,6}$"); name:str=Field(min_length=1,max_length=100); email:str=Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:str=Field(min_length=1,max_length=50); shift_start:str="09:30"; shift_end:str="18:30"; joined_on:str
    @field_validator('joined_on')
    @classmethod
    def valid_join(cls,v): return date_value(v)
    @field_validator('shift_start','shift_end')
    @classmethod
    def valid_shift(cls,v):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",v): raise ValueError('time must be HH:MM')
        return v
    @model_validator(mode='after')
    def different(self):
        if self.shift_start==self.shift_end: raise ValueError('shift times must differ')
        return self
class PunchInIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str; punched_at:Optional[StrictInt]=None; status:Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
    @field_validator('punched_at')
    @classmethod
    def ts(cls,v):
        if v is not None: parse_ms(v)
        return v
class PunchOutIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    emp_code:str; punched_at:Optional[StrictInt]=None
    @field_validator('punched_at')
    @classmethod
    def ts(cls,v):
        if v is not None: parse_ms(v)
        return v
class PatchIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    status:Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None
    punch_in:Optional[StrictInt]=None; punch_out:Optional[StrictInt]=None; reason:str=Field(min_length=5,max_length=200); regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator('punch_in','punch_out')
    @classmethod
    def ts(cls,v):
        if v is not None: parse_ms(v)
        return v
    @model_validator(mode='after')
    def reject_null_times(self):
        if ('punch_in' in self.model_fields_set and self.punch_in is None) or ('punch_out' in self.model_fields_set and self.punch_out is None):
            raise ValueError('punch times must be epoch milliseconds')
        return self

@system_router.get('/health')
def health():
    try: client.admin.command('ping'); return {'status':'ok'}
    except Exception: raise HTTPException(503,'database unavailable')

@employee_router.post('',status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc['created_at']=now_dt()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'emp_code already exists')
    doc.pop('_id',None)
    doc['created_at']=epoch(doc['created_at']); return doc

@employee_router.get('')
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department is not None else {}; total=db.employees.count_documents(q)
    docs=list(db.employees.find(q,{'_id':0}).sort('emp_code',1).skip((page-1)*page_size).limit(page_size))
    for d in docs: d['created_at']=epoch(d['created_at'])
    return {'items':docs,'total':total,'page':page,'page_size':page_size}

@attendance_router.post('/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=body.punched_at if isinstance(body.punched_at,datetime) else parse_ms(body.punched_at) if body.punched_at else now_dt()
    day=attendance_day(ts,emp); doc={'emp_code':body.emp_code,'date':day,'status':body.status,'punch_in':ts,'punch_out':None,'work_hours':None,'late_minutes':late_value(ts,emp,day),'overtime_minutes':0,'half_day':False,'history':[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'already punched in for this date')
    return api_record(doc)

@attendance_router.get('')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[Literal['PRESENT','ABSENT','LEAVE','WFH','ON_DUTY']]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code is not None:q['emp_code']=emp_code
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=safe_date(date_from)
        if date_to:q['date']['$lte']=safe_date(date_to)
        if date_from and date_to and date_from>date_to: raise HTTPException(422,'date_from must be <= date_to')
    if status:q['status']=status
    total=db.attendance_logs.count_documents(q); docs=db.attendance_logs.find(q).sort([('date',-1),('emp_code',1)]).skip((page-1)*page_size).limit(page_size)
    return {'items':[api_record(d) for d in docs],'total':total,'page':page,'page_size':page_size}

@attendance_router.post('/punch-out')
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    ts=body.punched_at if isinstance(body.punched_at,datetime) else parse_ms(body.punched_at) if body.punched_at else now_dt()
    rec=db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$lte':ts},'punch_out':None},sort=[('punch_in',-1)])
    if rec is None:
        if db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$lte':ts}}): raise HTTPException(409,'already punched out')
        raise HTTPException(404,'no punch-in found')
    delta=(ts-rec['punch_in']).total_seconds()
    if delta<=0 or delta>86400: raise HTTPException(422,'punch_out must be after punch_in and within 24 hours')
    hours,overtime,half=work_values(rec['punch_in'],ts,emp,rec['date'])
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'],'punch_out':None},{'$set':{'punch_out':ts,'work_hours':hours,'overtime_minutes':overtime,'half_day':half}},return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,'already punched out')
    return api_record(updated)

@attendance_router.patch('/{emp_code}/{date}')
def regularize(emp_code:str,date:str,body:PatchIn):
    try: day=date_value(date)
    except ValueError as e: raise HTTPException(422,str(e))
    emp=db.employees.find_one({'emp_code':emp_code}); rec=db.attendance_logs.find_one({'emp_code':emp_code,'date':day})
    if not emp or not rec: raise HTTPException(404,'employee or attendance record not found')
    old={'status':rec.get('status'),'punch_in':rec.get('punch_in'),'punch_out':rec.get('punch_out'),
         'work_hours':rec.get('work_hours'),'late_minutes':rec.get('late_minutes',0),
         'overtime_minutes':rec.get('overtime_minutes',0),'half_day':rec.get('half_day',False)}
    status=body.status or rec['status']; pin=parse_ms(body.punch_in) if 'punch_in' in body.model_fields_set else rec.get('punch_in'); pout=parse_ms(body.punch_out) if 'punch_out' in body.model_fields_set else rec.get('punch_out')
    if status in ('ABSENT','LEAVE'):
        if 'punch_in' in body.model_fields_set or 'punch_out' in body.model_fields_set: raise HTTPException(422,'absence/leave cannot include punch times')
        pin=pout=None
    elif pin is None: raise HTTPException(422,'presence status requires punch_in')
    if pin is not None and attendance_day(pin,emp)!=day: raise HTTPException(422,'punch_in must belong to attendance date')
    if pout is not None and (pin is None or (pout-pin).total_seconds()<=0 or (pout-pin).total_seconds()>86400): raise HTTPException(422,'invalid punch_out')
    late=late_value(pin,emp,day) if pin and status in PRESENCE else 0
    hours,overtime,half=work_values(pin,pout,emp,day) if pin and pout and status in PRESENCE else (None,0,False)
    new={'status':status,'punch_in':pin,'punch_out':pout,'work_hours':hours,'late_minutes':late,'overtime_minutes':overtime,'half_day':half}
    changes={k:{'from':old[k],'to':v} for k,v in new.items() if old[k]!=v}
    if not changes: raise HTTPException(422,'request changes nothing')
    entry={'at':now_dt(),'by':body.regularized_by,'reason':body.reason,'changes':changes}
    history_guard={'history':rec['history']} if 'history' in rec else {'history':{'$exists':False}}
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'],**history_guard},{'$set':new,'$push':{'history':entry}},return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,'record changed concurrently')
    return api_record(updated)

# All reporting calculations below run in MongoDB pipelines; only formatting and validation happen in Python.
def month_match(month):
    try:return month_bounds(month)
    except ValueError as e: raise HTTPException(422,str(e))
def monthly_pipeline(emp_code,month):
    first,last=month_bounds(month)
    return [{'$match':{'emp_code':emp_code,'date':{'$gte':first,'$lte':last}}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$in':['$status',PRESENCE]},{'$cond':[{'$eq':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},1]},0,{'$cond':[{'$eq':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},7]},0,{'$cond':[{'$eq':[{'$ifNull':['$half_day',False]},True]},0.5,1]}]}]},0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}]
def working_days_pipeline(emp_code,month):
    first,last=month_bounds(month)
    return [
        {'$match':{'emp_code':emp_code}},
        {'$set':{'period_start':{'$cond':[{'$gt':['$joined_on',first]},'$joined_on',first]}}},
        {'$set':{'day_count':{'$cond':[{'$gt':['$period_start',last]},-1,{'$dateDiff':{'startDate':{'$dateFromString':{'dateString':'$period_start'}},'endDate':{'$dateFromString':{'dateString':last}},'unit':'day'}}]}}},
        {'$set':{'days':{'$map':{'input':{'$range':[0,{'$add':['$day_count',1]}]},'as':'offset','in':{'$dateAdd':{'startDate':{'$dateFromString':{'dateString':'$period_start'}},'unit':'day','amount':'$$offset'}}}}}},
        {'$project':{'working_days':{'$size':{'$filter':{'input':'$days','as':'d','cond':{'$not':[{'$in':[{'$dayOfWeek':'$$d'},[1,7]]}]}}}}}}
    ]

@analytics_router.get('/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$')):
    emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'employee not found')
    rows=list(db.attendance_logs.aggregate(monthly_pipeline(emp_code,month))); a=rows[0] if rows else {}
    wd_rows=list(db.employees.aggregate(working_days_pipeline(emp_code,month))); wd=wd_rows[0]['working_days'] if wd_rows else 0; present=a.get('present_days',0)
    return {'emp_code':emp_code,'month':month,'working_days':wd,'present_days':round2(present),'leave_days':a.get('leave_days',0),'late_count':a.get('late_count',0),'total_late_minutes':a.get('total_late_minutes',0),'total_overtime_minutes':a.get('total_overtime_minutes',0),'attendance_pct':round2(present/wd*100) if wd else None}

def dept_summary_pipeline(month,department=None):
    first,last=month_bounds(month); match={'joined_on':{'$lte':last}}
    if department is not None:match['department']=department
    nonweekend={'$not':[{'$in':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},[1,7]]}]}
    present={'$and':[{'$in':['$status',PRESENCE]},nonweekend]}
    stats_pipeline=[
        {'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',first]},{'$lte':['$date',last]}]}}},
        {'$group':{'_id':None,
            'present_days':{'$sum':{'$cond':[present,{'$cond':[{'$eq':[{'$ifNull':['$half_day',False]},True]},0.5,1]},0]}},
            'hours_sum':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENCE]},{'$ne':['$work_hours',None]}]},'$work_hours',0]}},
            'hours_count':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENCE]},{'$ne':['$work_hours',None]}]},1,0]}},
            'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},
            'late_total':{'$sum':{'$ifNull':['$late_minutes',0]}},
            'leave_count':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},
            'on_duty_count':{'$sum':{'$cond':[{'$eq':['$status','ON_DUTY']},1,0]}}}}
    ]
    return [
        {'$match':match},
        {'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code'},'pipeline':stats_pipeline,'as':'stats'}},
        {'$set':{'stats':{'$ifNull':[{'$arrayElemAt':['$stats',0]},{'present_days':0,'hours_sum':0,'hours_count':0,'late_count':0,'late_total':0,'leave_count':0,'on_duty_count':0}]}}},
        {'$group':{'_id':'$department','headcount':{'$sum':1},'present_days':{'$sum':'$stats.present_days'},'hours_sum':{'$sum':'$stats.hours_sum'},'hours_count':{'$sum':'$stats.hours_count'},'late_count':{'$sum':'$stats.late_count'},'total_late_minutes':{'$sum':'$stats.late_total'},'leave_count':{'$sum':'$stats.leave_count'},'on_duty_count':{'$sum':'$stats.on_duty_count'}}},
        {'$project':{'_id':0,'department':'$_id','headcount':1,'present_days':1,'avg_work_hours':{'$cond':[{'$gt':['$hours_count',0]},{'$divide':['$hours_sum','$hours_count']},None]},'late_count':1,'total_late_minutes':1,'leave_count':1,'on_duty_count':1}},
        {'$sort':{'department':1}}
    ]
@analytics_router.get('/departments/summary')
def department_summary(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),department:Optional[str]=None):
    month_match(month)
    items=list(db.employees.aggregate(dept_summary_pipeline(month,department)))
    for item in items:
        item['present_days']=round2(item['present_days'])
        if item['avg_work_hours'] is not None: item['avg_work_hours']=round2(item['avg_work_hours'])
    return {'month':month,'items':items}

def late_pipeline(month,department=None):
    first,last=month_bounds(month); match={'date':{'$gte':first,'$lte':last},'late_minutes':{'$gt':0}}
    pipe=[{'$match':match},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'},'late_count':{'$sum':1}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'}]
    if department is not None:pipe.append({'$match':{'employee.department':department}})
    pipe += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':50}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$employee.name','department':'$employee.department','total_late_minutes':1,'late_count':1}}]
    return pipe
@analytics_router.get('/leaderboard/late')
def late_leaderboard(month:str=Query(...,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    month_match(month); items=list(db.attendance_logs.aggregate(late_pipeline(month,department)))
    return {'month':month,'items':[x for x in items if x['rank']<=limit]}

def trend_pipeline(department,start,end):
    # Generate a calendar spine with $range, then derive metrics with indexed lookups in MongoDB.
    n=(date.fromisoformat(end)-date.fromisoformat(start)).days+1
    calendar={'$map':{'input':{'$range':[0,n]},'as':'i','in':{'$dateToString':{'date':{'$dateAdd':{'startDate':{'$dateFromString':{'dateString':start}},'unit':'day','amount':'$$i'}},'format':'%Y-%m-%d'}}}}
    emp_hc=[{'$match':{'department':department,'$expr':{'$lte':['$joined_on','$$d']}}},{'$count':'n'}]
    attendance=[
        {'$match':{'$expr':{'$eq':['$date','$$d']}}},
        {'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'e'}},
        {'$unwind':'$e'}, {'$match':{'e.department':department}},
        {'$group':{'_id':None,
            'present':{'$sum':{'$cond':[{'$in':['$status',PRESENCE]},{'$cond':[{'$eq':[{'$ifNull':['$half_day',False]},True]},0.5,1]},0]}},
            'late':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}}
    ]
    return [
        {'$match':{'department':department}}, {'$limit':1}, {'$project':{'_id':0,'days':calendar}},
        {'$unwind':'$days'}, {'$project':{'date':'$days'}},
        {'$lookup':{'from':'employees','let':{'d':'$date'},'pipeline':emp_hc,'as':'hc'}},
        {'$lookup':{'from':'attendance_logs','let':{'d':'$date'},'pipeline':attendance,'as':'a'}},
        {'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]},'present_count':{'$ifNull':[{'$arrayElemAt':['$a.present',0]},0]},'late_count':{'$ifNull':[{'$arrayElemAt':['$a.late',0]},0]},'is_working_day':{'$not':[{'$in':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},[1,7]]}]}}},
        {'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},{'$divide':['$present_count','$headcount']},None]}}},
        {'$setWindowFields':{'sortBy':{'date':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},
        {'$project':{'_id':0,'date':1,'is_working_day':1,'headcount':1,'present_count':1,'late_count':1,'attendance_rate':1,'moving_avg_7d':1}}
    ]
@analytics_router.get('/departments/{department}/trend')
def department_trend(department:str,from_:str=Query(...,alias='from'),to:str=Query(...)):
    try: start=date_value(from_); end=date_value(to)
    except ValueError as e: raise HTTPException(422,str(e))
    days=(date.fromisoformat(end)-date.fromisoformat(start)).days
    if days<0 or days>91: raise HTTPException(422,'range must be 1 to 92 days')
    if not db.employees.find_one({'department':department}): raise HTTPException(404,'department not found')
    items=list(db.employees.aggregate(trend_pipeline(department,start,end)))
    for item in items:
        if item['attendance_rate'] is not None: item['attendance_rate']=round4(item['attendance_rate'])
        if item['moving_avg_7d'] is not None: item['moving_avg_7d']=round4(item['moving_avg_7d'])
    return {'department':department,'items':items}

@admin_router.get('/explain/{endpoint}')
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_:Optional[str]=Query(None,alias='from'),to:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if endpoint=='attendance_list':
        q={}
        if emp_code:q['emp_code']=emp_code
        if date_from:q.setdefault('date',{})['$gte']=safe_date(date_from)
        if date_to:q.setdefault('date',{})['$lte']=safe_date(date_to)
        if status:q['status']=status
        result=db.command('explain',{'find':'attendance_logs','filter':q,'sort':{'date':-1,'emp_code':1},'skip':(page-1)*page_size,'limit':page_size},verbosity='executionStats'); collection='attendance_logs'
    elif endpoint=='department_trend':
        if not department or not from_ or not to:raise HTTPException(422,'department, from and to are required')
        start=date_value(from_);end=date_value(to)
        if (date.fromisoformat(end)-date.fromisoformat(start)).days not in range(0,92):raise HTTPException(422,'invalid range')
        query={'aggregate':'employees','pipeline':trend_pipeline(department,start,end),'cursor':{}};collection='employees'
        result=db.command('explain',query,verbosity='executionStats')
    else:
        if not month:raise HTTPException(422,'month is required')
        month_match(month)
        if endpoint=='employee_monthly':
            if not emp_code:raise HTTPException(422,'emp_code is required')
            query={'aggregate':'attendance_logs','pipeline':monthly_pipeline(emp_code,month),'cursor':{}};collection='attendance_logs'
        elif endpoint=='department_summary':query={'aggregate':'employees','pipeline':dept_summary_pipeline(month,department),'cursor':{}};collection='employees'
        elif endpoint=='late_leaderboard':query={'aggregate':'attendance_logs','pipeline':late_pipeline(month,department),'cursor':{}};collection='attendance_logs'
        else:raise HTTPException(422,'unknown endpoint')
        result=db.command('explain',query,verbosity='executionStats')
    from bson import json_util
    return {'endpoint':endpoint,'collection':collection,'explain':json_util.loads(json_util.dumps(result))}


# Keep URL registration in one place; each router owns one API area.
app.include_router(system_router)
app.include_router(employee_router)
app.include_router(attendance_router)
app.include_router(analytics_router)
app.include_router(admin_router)
