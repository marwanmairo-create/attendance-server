from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import sqlite3
from datetime import datetime, date
import io
import csv

app = FastAPI(title="Employee Attendance API")

DB_NAME = "attendance.db"


def get_db():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        password TEXT NOT NULL,
        device_id TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_code TEXT NOT NULL,
        attendance_date TEXT NOT NULL,
        check_in_time TEXT,
        check_out_time TEXT,
        check_in_lat REAL,
        check_in_lng REAL,
        check_out_lat REAL,
        check_out_lng REAL,
        status TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


init_db()


class EmployeeCreate(BaseModel):
    employee_code: str
    name: str
    phone: str | None = None
    password: str
    device_id: str | None = None


class LoginRequest(BaseModel):
    employee_code: str
    password: str
    device_id: str | None = None


class AttendanceRequest(BaseModel):
    employee_code: str
    lat: float | None = None
    lng: float | None = None
    device_id: str | None = None


WORK_START = "11:00"
WORK_END = "23:00"
GRACE_MINUTES = 5
REQUIRED_WORK_HOURS = 12
LATE_DEDUCTION = 50
OVERTIME_HOUR_RATE = 50


def now_time():
    return datetime.now().strftime("%H:%M:%S")


def today_date():
    return date.today().isoformat()


def is_late(check_in_time: str):
    start = datetime.strptime(WORK_START, "%H:%M")
    check = datetime.strptime(check_in_time[:5], "%H:%M")
    diff_minutes = int((check - start).total_seconds() / 60)
    if diff_minutes <= GRACE_MINUTES:
        return False, 0
    return True, diff_minutes


def calculate_day_values(check_in_time, check_out_time):
    late_minutes = 0
    late_deduction = 0
    worked_hours = 0
    overtime_hours = 0
    overtime_amount = 0
    net_amount = 0

    if check_in_time:
        start = datetime.strptime(WORK_START, "%H:%M")
        check = datetime.strptime(check_in_time[:5], "%H:%M")
        diff_minutes = int((check - start).total_seconds() / 60)
        if diff_minutes > GRACE_MINUTES:
            late_minutes = diff_minutes
            late_deduction = LATE_DEDUCTION

    if check_in_time and check_out_time:
        check_in_dt = datetime.strptime(check_in_time, "%H:%M:%S")
        check_out_dt = datetime.strptime(check_out_time, "%H:%M:%S")
        work_minutes = int((check_out_dt - check_in_dt).total_seconds() / 60)
        if work_minutes < 0:
            work_minutes += 24 * 60
        worked_hours = round(work_minutes / 60, 2)
        required_minutes = REQUIRED_WORK_HOURS * 60
        if work_minutes > required_minutes:
            overtime_minutes = work_minutes - required_minutes
            overtime_hours = round(overtime_minutes / 60, 2)
            overtime_amount = round(overtime_hours * OVERTIME_HOUR_RATE, 2)

    net_amount = overtime_amount - late_deduction
    return {
        "worked_hours": worked_hours,
        "late_minutes": late_minutes,
        "late_deduction": late_deduction,
        "overtime_hours": overtime_hours,
        "overtime_amount": overtime_amount,
        "net_amount": net_amount,
    }


@app.get("/")
def home():
    return {"message": "Attendance server is running", "status": "online"}


@app.post("/employees/create")
def create_employee(employee: EmployeeCreate):
    conn = get_db()
    cur = conn.cursor()
    device_id = employee.device_id.strip() if employee.device_id else None
    try:
        cur.execute(
            """
            INSERT INTO employees (employee_code, name, phone, password, device_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                employee.employee_code.strip(),
                employee.name.strip(),
                employee.phone.strip() if employee.phone else None,
                employee.password.strip(),
                device_id or None,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="كود الموظف موجود بالفعل")
    conn.close()
    return {"success": True, "message": "تم إضافة الموظف بنجاح"}


@app.post("/login")
def login(data: LoginRequest):
    conn = get_db()
    cur = conn.cursor()
    employee_code = data.employee_code.strip()
    password = data.password.strip()
    device_id = data.device_id.strip() if data.device_id else None

    cur.execute(
        """
        SELECT employee_code, name, device_id
        FROM employees
        WHERE employee_code = ? AND password = ?
        """,
        (employee_code, password),
    )
    employee = cur.fetchone()
    conn.close()

    if not employee:
        raise HTTPException(status_code=401, detail="بيانات الدخول غير صحيحة")

    saved_device = employee[2]
    if saved_device and device_id and saved_device != device_id:
        raise HTTPException(status_code=403, detail="هذا الموظف مربوط بجهاز آخر")

    return {
        "success": True,
        "employee_code": employee[0],
        "name": employee[1],
        "message": "تم تسجيل الدخول بنجاح",
    }


@app.post("/attendance/check-in")
def check_in(data: AttendanceRequest):
    conn = get_db()
    cur = conn.cursor()
    employee_code = data.employee_code.strip()
    device_id = data.device_id.strip() if data.device_id else None

    cur.execute("SELECT employee_code, device_id FROM employees WHERE employee_code = ?", (employee_code,))
    employee = cur.fetchone()
    if not employee:
        conn.close()
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    saved_device = employee[1]
    if saved_device and device_id and saved_device != device_id:
        conn.close()
        raise HTTPException(status_code=403, detail="الجهاز غير مصرح له")

    attendance_date = today_date()
    cur.execute(
        "SELECT id FROM attendance WHERE employee_code = ? AND attendance_date = ?",
        (employee_code, attendance_date),
    )
    existing = cur.fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="تم تسجيل الحضور بالفعل اليوم")

    check_time = now_time()
    late, late_minutes = is_late(check_time)
    status = "حاضر" if not late else f"متأخر {late_minutes} دقيقة"

    cur.execute(
        """
        INSERT INTO attendance
        (employee_code, attendance_date, check_in_time, check_in_lat, check_in_lng, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (employee_code, attendance_date, check_time, data.lat, data.lng, status, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "تم تسجيل الحضور بنجاح",
        "date": attendance_date,
        "check_in_time": check_time,
        "status": status,
    }


@app.post("/attendance/check-out")
def check_out(data: AttendanceRequest):
    conn = get_db()
    cur = conn.cursor()
    employee_code = data.employee_code.strip()
    device_id = data.device_id.strip() if data.device_id else None

    cur.execute("SELECT employee_code, device_id FROM employees WHERE employee_code = ?", (employee_code,))
    employee = cur.fetchone()
    if not employee:
        conn.close()
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    saved_device = employee[1]
    if saved_device and device_id and saved_device != device_id:
        conn.close()
        raise HTTPException(status_code=403, detail="الجهاز غير مصرح له")

    attendance_date = today_date()
    cur.execute(
        "SELECT id, check_out_time FROM attendance WHERE employee_code = ? AND attendance_date = ?",
        (employee_code, attendance_date),
    )
    record = cur.fetchone()
    if not record:
        conn.close()
        raise HTTPException(status_code=404, detail="لم يتم تسجيل حضور اليوم")
    if record[1]:
        conn.close()
        raise HTTPException(status_code=400, detail="تم تسجيل الانصراف بالفعل")

    out_time = now_time()
    cur.execute(
        """
        UPDATE attendance
        SET check_out_time = ?, check_out_lat = ?, check_out_lng = ?
        WHERE id = ?
        """,
        (out_time, data.lat, data.lng, record[0]),
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "تم تسجيل الانصراف بنجاح", "check_out_time": out_time}


@app.get("/attendance/today")
def get_today_attendance():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT a.employee_code, e.name, a.attendance_date, a.check_in_time, a.check_out_time, a.status
        FROM attendance a
        LEFT JOIN employees e ON a.employee_code = e.employee_code
        WHERE a.attendance_date = ?
        ORDER BY a.check_in_time ASC
        """,
        (today_date(),),
    )
    rows = cur.fetchall()
    conn.close()
    data = []
    for row in rows:
        values = calculate_day_values(row[3], row[4])
        data.append({
            "employee_code": row[0],
            "name": row[1],
            "date": row[2],
            "check_in": row[3],
            "check_out": row[4],
            "status": row[5],
            **values,
        })
    return {"success": True, "date": today_date(), "attendance": data}


@app.get("/employees")
def get_employees():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT employee_code, name, phone, device_id FROM employees ORDER BY name ASC")
    rows = cur.fetchall()
    conn.close()
    employees = []
    for row in rows:
        employees.append({"employee_code": row[0], "name": row[1], "phone": row[2], "device_id": row[3]})
    return {"success": True, "employees": employees}


@app.post("/employees/remove-devices")
def remove_all_devices():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE employees SET device_id = NULL")
    conn.commit()
    conn.close()
    return {"success": True, "message": "تم فك ربط الأجهزة من كل الموظفين"}


@app.get("/attendance/monthly")
def get_monthly_attendance(employee_code: str | None = None, month: str | None = None):
    if not month:
        month = datetime.now().strftime("%Y-%m")
    conn = get_db()
    cur = conn.cursor()
    if employee_code:
        cur.execute(
            """
            SELECT a.employee_code, e.name, a.attendance_date, a.check_in_time, a.check_out_time, a.status
            FROM attendance a
            LEFT JOIN employees e ON a.employee_code = e.employee_code
            WHERE a.attendance_date LIKE ? AND a.employee_code = ?
            ORDER BY a.attendance_date ASC
            """,
            (month + "%", employee_code.strip()),
        )
    else:
        cur.execute(
            """
            SELECT a.employee_code, e.name, a.attendance_date, a.check_in_time, a.check_out_time, a.status
            FROM attendance a
            LEFT JOIN employees e ON a.employee_code = e.employee_code
            WHERE a.attendance_date LIKE ?
            ORDER BY a.attendance_date ASC, a.employee_code ASC
            """,
            (month + "%",),
        )
    rows = cur.fetchall()
    conn.close()

    data = []
    totals = {
        "total_worked_hours": 0,
        "total_late_minutes": 0,
        "total_late_deduction": 0,
        "total_overtime_hours": 0,
        "total_overtime_amount": 0,
        "total_net_amount": 0,
    }
    for row in rows:
        values = calculate_day_values(row[3], row[4])
        totals["total_worked_hours"] += values["worked_hours"]
        totals["total_late_minutes"] += values["late_minutes"]
        totals["total_late_deduction"] += values["late_deduction"]
        totals["total_overtime_hours"] += values["overtime_hours"]
        totals["total_overtime_amount"] += values["overtime_amount"]
        totals["total_net_amount"] += values["net_amount"]
        data.append({
            "employee_code": row[0],
            "name": row[1],
            "date": row[2],
            "check_in": row[3],
            "check_out": row[4],
            "status": row[5],
            **values,
        })

    return {
        "success": True,
        "month": month,
        "employee_code": employee_code,
        "summary": {
            "days_count": len(data),
            "total_worked_hours": round(totals["total_worked_hours"], 2),
            "total_late_minutes": totals["total_late_minutes"],
            "total_late_deduction": totals["total_late_deduction"],
            "total_overtime_hours": round(totals["total_overtime_hours"], 2),
            "total_overtime_amount": round(totals["total_overtime_amount"], 2),
            "total_net_amount": round(totals["total_net_amount"], 2),
        },
        "attendance": data,
    }


@app.get("/attendance/export-monthly")
def export_monthly_attendance(employee_code: str | None = None, month: str | None = None):
    if not month:
        month = datetime.now().strftime("%Y-%m")
    conn = get_db()
    cur = conn.cursor()
    if employee_code:
        cur.execute(
            """
            SELECT a.employee_code, e.name, a.attendance_date, a.check_in_time, a.check_out_time, a.status
            FROM attendance a
            LEFT JOIN employees e ON a.employee_code = e.employee_code
            WHERE a.attendance_date LIKE ? AND a.employee_code = ?
            ORDER BY a.attendance_date ASC
            """,
            (month + "%", employee_code.strip()),
        )
    else:
        cur.execute(
            """
            SELECT a.employee_code, e.name, a.attendance_date, a.check_in_time, a.check_out_time, a.status
            FROM attendance a
            LEFT JOIN employees e ON a.employee_code = e.employee_code
            WHERE a.attendance_date LIKE ?
            ORDER BY a.attendance_date ASC, a.employee_code ASC
            """,
            (month + "%",),
        )
    rows = cur.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["كود الموظف", "اسم الموظف", "التاريخ", "وقت الحضور", "وقت الانصراف", "ساعات العمل", "دقائق التأخير", "خصم التأخير", "ساعات الإضافي", "قيمة الإضافي", "صافي اليوم", "الحالة"])
    for row in rows:
        values = calculate_day_values(row[3], row[4])
        writer.writerow([row[0], row[1], row[2], row[3], row[4], values["worked_hours"], values["late_minutes"], values["late_deduction"], values["overtime_hours"], values["overtime_amount"], values["net_amount"], row[5]])
    output.seek(0)
    file_name = f"monthly_attendance_{month}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={file_name}"},
    )


@app.get("/mobile", response_class=HTMLResponse)
def mobile_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>تسجيل حضور الموظفين</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{font-family:Arial;background:#f2f4f8;padding:20px;direction:rtl}.box{max-width:420px;margin:auto;background:white;padding:20px;border-radius:14px;box-shadow:0 3px 12px rgba(0,0,0,.15)}h2{text-align:center}input{width:100%;padding:13px;margin-bottom:12px;border:1px solid #ccc;border-radius:8px;font-size:16px;box-sizing:border-box}button{width:100%;padding:14px;margin-top:10px;border:none;border-radius:8px;font-size:17px;color:white;cursor:pointer}.login{background:#2563eb}.in{background:#16a34a}.out{background:#dc2626}.msg{margin-top:15px;padding:12px;background:#eef2ff;border-radius:8px;text-align:center;min-height:25px}
</style>
</head>
<body>
<div class="box">
<h2>تسجيل حضور الموظفين</h2>
<input id="employee_code" placeholder="كود الموظف">
<input id="password" placeholder="كلمة السر" type="password">
<input id="device_id" placeholder="كود الجهاز اختياري">
<button class="login" onclick="login()">تسجيل الدخول</button>
<button class="in" onclick="checkIn()">تسجيل حضور</button>
<button class="out" onclick="checkOut()">تسجيل انصراف</button>
<div class="msg" id="message">جاهز</div>
</div>
<script>
let loggedEmployee=null;
function showMessage(text){document.getElementById("message").innerText=text;}
async function login(){
 const employee_code=document.getElementById("employee_code").value.trim();
 const password=document.getElementById("password").value.trim();
 const device_id=document.getElementById("device_id").value.trim();
 if(!employee_code||!password){showMessage("اكتب كود الموظف وكلمة السر");return;}
 try{
  const res=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({employee_code,password,device_id:device_id||null})});
  const data=await res.json();
  if(!res.ok){showMessage(data.detail||"فشل تسجيل الدخول");return;}
  loggedEmployee=employee_code;
  showMessage("تم تسجيل الدخول: "+data.name);
 }catch(e){showMessage("خطأ في الاتصال بالسيرفر");}
}
function checkIn(){sendAttendance("/attendance/check-in");}
function checkOut(){sendAttendance("/attendance/check-out");}
function sendAttendance(url){
 if(!loggedEmployee){showMessage("سجل الدخول الأول");return;}
 showMessage("جاري تحديد الموقع...");
 if(navigator.geolocation){
  navigator.geolocation.getCurrentPosition(
   p=>sendToServer(url,p.coords.latitude,p.coords.longitude),
   ()=>sendToServer(url,null,null),
   {enableHighAccuracy:true,timeout:8000,maximumAge:0}
  );
 }else{sendToServer(url,null,null);}
}
async function sendToServer(url,lat,lng){
 const device_id=document.getElementById("device_id").value.trim();
 try{
  const res=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({employee_code:loggedEmployee,lat,lng,device_id:device_id||null})});
  const data=await res.json();
  if(!res.ok){showMessage(data.detail||"حدث خطأ");return;}
  showMessage(data.message);
 }catch(e){showMessage("خطأ في الاتصال بالسيرفر");}
}
</script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>لوحة إدارة الحضور</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{font-family:Arial;background:#f3f4f6;padding:20px;direction:rtl}.box{background:white;padding:20px;border-radius:14px;box-shadow:0 3px 12px rgba(0,0,0,.12);margin-bottom:20px}h2,h3{text-align:center}input{width:100%;padding:12px;margin-bottom:10px;border:1px solid #ccc;border-radius:8px;font-size:15px;box-sizing:border-box}button{padding:12px 18px;background:#2563eb;color:white;border:none;border-radius:8px;margin:5px 0;font-size:16px;cursor:pointer}.add-btn{background:#16a34a;width:100%}.danger-btn{background:#dc2626;width:100%}.refresh-btn{background:#2563eb}.monthly-link{display:block;background:#7c3aed;color:white;text-align:center;padding:14px;border-radius:8px;text-decoration:none;font-size:17px;font-weight:bold;margin-top:10px}.msg{margin-top:10px;padding:10px;background:#eef2ff;border-radius:8px;text-align:center}.table-wrap{width:100%;overflow-x:auto}table{width:100%;border-collapse:collapse;background:white;margin-top:10px;min-width:950px}th{background:#111827;color:white;padding:12px;white-space:nowrap}td{padding:11px;border-bottom:1px solid #ddd;text-align:center;white-space:nowrap}tr:nth-child(even){background:#f9fafb}.status{font-weight:bold;color:#16a34a}
</style>
</head>
<body>
<div class="box"><h2>لوحة إدارة الحضور</h2><a class="monthly-link" href="/monthly">فتح التقرير الشهري</a><a class="monthly-link" href="/payroll">المرتبات والسلف والجزاءات</a><a class="monthly-link" href="/employee-report">تقرير عامل مفصل</a></div>
<div class="box"><h3>إضافة موظف جديد</h3><input id="new_code" placeholder="كود الموظف"><input id="new_name" placeholder="اسم الموظف"><input id="new_phone" placeholder="رقم الهاتف"><input id="new_password" placeholder="كلمة السر"><input id="new_device" placeholder="كود الجهاز اختياري"><button class="add-btn" onclick="addEmployee()">إضافة الموظف</button><button class="danger-btn" onclick="removeAllDevices()">فك ربط الأجهزة من كل الموظفين</button><div class="msg" id="employee_msg">جاهز لإضافة موظف</div></div>
<div class="box"><h3>حضور اليوم</h3><button class="refresh-btn" onclick="loadAttendance()">تحديث بيانات الحضور</button><div class="table-wrap"><table><thead><tr><th>كود الموظف</th><th>الاسم</th><th>التاريخ</th><th>الحضور</th><th>الانصراف</th><th>ساعات العمل</th><th>دقائق التأخير</th><th>خصم التأخير</th><th>ساعات الإضافي</th><th>قيمة الإضافي</th><th>صافي اليوم</th><th>الحالة</th></tr></thead><tbody id="attendance_body"><tr><td colspan="12">جاري تحميل البيانات...</td></tr></tbody></table></div></div>
<div class="box"><h3>قائمة الموظفين</h3><button class="refresh-btn" onclick="loadEmployees()">تحديث قائمة الموظفين</button><div class="table-wrap"><table><thead><tr><th>كود الموظف</th><th>الاسم</th><th>الهاتف</th><th>كود الجهاز</th></tr></thead><tbody id="employees_body"><tr><td colspan="4">جاري تحميل الموظفين...</td></tr></tbody></table></div></div>
<script>
function showEmployeeMessage(text){document.getElementById("employee_msg").innerText=text;}
async function addEmployee(){
 const employee_code=document.getElementById("new_code").value.trim(),name=document.getElementById("new_name").value.trim(),phone=document.getElementById("new_phone").value.trim(),password=document.getElementById("new_password").value.trim(),device_id=document.getElementById("new_device").value.trim();
 if(!employee_code||!name||!password){showEmployeeMessage("اكتب كود الموظف والاسم وكلمة السر");return;}
 try{const res=await fetch("/employees/create",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({employee_code,name,phone,password,device_id:device_id||null})});const data=await res.json();if(!res.ok){showEmployeeMessage(data.detail||"حدث خطأ أثناء إضافة الموظف");return;}showEmployeeMessage("تم إضافة الموظف بنجاح");document.getElementById("new_code").value="";document.getElementById("new_name").value="";document.getElementById("new_phone").value="";document.getElementById("new_password").value="";document.getElementById("new_device").value="";loadEmployees();}catch(e){showEmployeeMessage("خطأ في الاتصال بالسيرفر");}
}
async function removeAllDevices(){
 if(!confirm("هل تريد فك ربط الأجهزة من كل الموظفين؟")){return;}
 try{const res=await fetch("/employees/remove-devices",{method:"POST"});const data=await res.json();if(!res.ok){showEmployeeMessage(data.detail||"حدث خطأ");return;}showEmployeeMessage(data.message);loadEmployees();}catch(e){showEmployeeMessage("خطأ في الاتصال بالسيرفر");}
}
async function loadAttendance(){
 const tbody=document.getElementById("attendance_body");
 try{const res=await fetch("/attendance/today");const data=await res.json();tbody.innerHTML="";if(!data.attendance||data.attendance.length===0){tbody.innerHTML="<tr><td colspan='12'>لا يوجد حضور اليوم</td></tr>";return;}data.attendance.forEach(row=>{tbody.innerHTML+=`<tr><td>${row.employee_code||""}</td><td>${row.name||""}</td><td>${row.date||""}</td><td>${row.check_in||""}</td><td>${row.check_out||""}</td><td>${row.worked_hours||0}</td><td>${row.late_minutes||0}</td><td>${row.late_deduction||0} ج</td><td>${row.overtime_hours||0}</td><td>${row.overtime_amount||0} ج</td><td>${row.net_amount||0} ج</td><td class="status">${row.status||""}</td></tr>`;});}catch(e){tbody.innerHTML="<tr><td colspan='12'>حدث خطأ في تحميل البيانات</td></tr>";}
}
async function loadEmployees(){
 const tbody=document.getElementById("employees_body");
 try{const res=await fetch("/employees");const data=await res.json();tbody.innerHTML="";if(!data.employees||data.employees.length===0){tbody.innerHTML="<tr><td colspan='4'>لا يوجد موظفين</td></tr>";return;}data.employees.forEach(row=>{tbody.innerHTML+=`<tr><td>${row.employee_code||""}</td><td>${row.name||""}</td><td>${row.phone||""}</td><td>${row.device_id||""}</td></tr>`;});}catch(e){tbody.innerHTML="<tr><td colspan='4'>حدث خطأ في تحميل الموظفين</td></tr>";}
}
loadAttendance();loadEmployees();
</script>
</body>
</html>
"""


@app.get("/monthly", response_class=HTMLResponse)
def monthly_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>التقرير الشهري للحضور</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{font-family:Arial;background:#f3f4f6;padding:20px;direction:rtl}.box{background:white;padding:20px;border-radius:14px;box-shadow:0 3px 12px rgba(0,0,0,.12);margin-bottom:20px}h2,h3{text-align:center}input{width:100%;padding:12px;margin-bottom:10px;border:1px solid #ccc;border-radius:8px;font-size:15px;box-sizing:border-box}button{padding:12px 18px;color:white;border:none;border-radius:8px;margin:5px 0;font-size:16px;cursor:pointer}.search-btn{background:#2563eb;width:100%}.export-btn{background:#16a34a;width:100%}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.card{background:#eef2ff;padding:12px;border-radius:10px;text-align:center;font-weight:bold}.table-wrap{width:100%;overflow-x:auto}table{width:100%;border-collapse:collapse;min-width:1000px}th{background:#111827;color:white;padding:12px;white-space:nowrap}td{padding:11px;border-bottom:1px solid #ddd;text-align:center;white-space:nowrap}tr:nth-child(even){background:#f9fafb}.status{font-weight:bold;color:#16a34a}.back-link{display:block;background:#2563eb;color:white;text-align:center;padding:14px;border-radius:8px;text-decoration:none;font-size:17px;font-weight:bold;margin-top:10px}
</style>
</head>
<body>
<div class="box"><h2>التقرير الشهري للحضور والانصراف</h2><a class="back-link" href="/admin">الرجوع إلى لوحة الإدارة</a></div>
<div class="box"><h3>اختيار التقرير</h3><input id="employee_code" placeholder="كود الموظف، اتركه فاضي لعرض كل الموظفين"><input id="month" type="month"><button class="search-btn" onclick="loadMonthlyReport()">عرض التقرير</button><button class="export-btn" onclick="exportMonthlyReport()">تصدير Excel</button></div>
<div class="box"><h3>ملخص الشهر</h3><div class="summary"><div class="card" id="days_count">عدد الأيام: 0</div><div class="card" id="total_worked_hours">ساعات العمل: 0</div><div class="card" id="total_late_minutes">دقائق التأخير: 0</div><div class="card" id="total_late_deduction">خصم التأخير: 0 ج</div><div class="card" id="total_overtime_hours">ساعات الإضافي: 0</div><div class="card" id="total_overtime_amount">قيمة الإضافي: 0 ج</div><div class="card" id="total_net_amount">الصافي: 0 ج</div></div></div>
<div class="box"><h3>تفاصيل التقرير</h3><div class="table-wrap"><table><thead><tr><th>كود الموظف</th><th>الاسم</th><th>التاريخ</th><th>الحضور</th><th>الانصراف</th><th>ساعات العمل</th><th>دقائق التأخير</th><th>خصم التأخير</th><th>ساعات الإضافي</th><th>قيمة الإضافي</th><th>صافي اليوم</th><th>الحالة</th></tr></thead><tbody id="monthly_body"><tr><td colspan="12">اختر الشهر واضغط عرض التقرير</td></tr></tbody></table></div></div>
<script>
function setCurrentMonth(){const m=document.getElementById("month"),n=new Date(),mm=String(n.getMonth()+1).padStart(2,"0");m.value=n.getFullYear()+"-"+mm;}
async function loadMonthlyReport(){
 const employee_code=document.getElementById("employee_code").value.trim(),month=document.getElementById("month").value,tbody=document.getElementById("monthly_body");
 if(!month){alert("اختار الشهر الأول");return;}
 let url="/attendance/monthly?month="+encodeURIComponent(month);if(employee_code){url+="&employee_code="+encodeURIComponent(employee_code);}
 try{const res=await fetch(url);const data=await res.json();tbody.innerHTML="";document.getElementById("days_count").innerText="عدد الأيام: "+data.summary.days_count;document.getElementById("total_worked_hours").innerText="ساعات العمل: "+data.summary.total_worked_hours;document.getElementById("total_late_minutes").innerText="دقائق التأخير: "+data.summary.total_late_minutes;document.getElementById("total_late_deduction").innerText="خصم التأخير: "+data.summary.total_late_deduction+" ج";document.getElementById("total_overtime_hours").innerText="ساعات الإضافي: "+data.summary.total_overtime_hours;document.getElementById("total_overtime_amount").innerText="قيمة الإضافي: "+data.summary.total_overtime_amount+" ج";document.getElementById("total_net_amount").innerText="الصافي: "+data.summary.total_net_amount+" ج";if(!data.attendance||data.attendance.length===0){tbody.innerHTML="<tr><td colspan='12'>لا توجد بيانات في هذا الشهر</td></tr>";return;}data.attendance.forEach(row=>{tbody.innerHTML+=`<tr><td>${row.employee_code||""}</td><td>${row.name||""}</td><td>${row.date||""}</td><td>${row.check_in||""}</td><td>${row.check_out||""}</td><td>${row.worked_hours||0}</td><td>${row.late_minutes||0}</td><td>${row.late_deduction||0} ج</td><td>${row.overtime_hours||0}</td><td>${row.overtime_amount||0} ج</td><td>${row.net_amount||0} ج</td><td class="status">${row.status||""}</td></tr>`;});}catch(e){tbody.innerHTML="<tr><td colspan='12'>حدث خطأ في تحميل التقرير</td></tr>";}
}
function exportMonthlyReport(){const employee_code=document.getElementById("employee_code").value.trim(),month=document.getElementById("month").value;if(!month){alert("اختار الشهر الأول");return;}let url="/attendance/export-monthly?month="+encodeURIComponent(month);if(employee_code){url+="&employee_code="+encodeURIComponent(employee_code);}window.open(url,"_blank");}
setCurrentMonth();loadMonthlyReport();
</script>
</body>
</html>
"""

# =========================
# Payroll / Finance System
# =========================

def ensure_payroll_schema():
    conn = get_db()
    cur = conn.cursor()

    # Add salary column if the employees table was created before payroll features.
    try:
        cur.execute("ALTER TABLE employees ADD COLUMN salary REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS finance_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_code TEXT NOT NULL,
        item_date TEXT NOT NULL,
        item_type TEXT NOT NULL,
        amount REAL NOT NULL,
        reason TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


ensure_payroll_schema()


class SalaryUpdate(BaseModel):
    employee_code: str
    salary: float


class FinanceItemCreate(BaseModel):
    employee_code: str
    item_date: str | None = None
    item_type: str
    amount: float
    reason: str | None = None


VALID_FINANCE_TYPES = {"advance", "penalty", "deduction", "bonus"}


def finance_type_ar(item_type):
    mapping = {
        "advance": "سلفة",
        "penalty": "جزاء",
        "deduction": "خصم",
        "bonus": "مكافأة",
    }
    return mapping.get(item_type, item_type)


def normalize_month(month):
    if not month:
        return datetime.now().strftime("%Y-%m")
    return month[:7]


def get_employee_list(employee_code=None):
    conn = get_db()
    cur = conn.cursor()

    if employee_code:
        cur.execute("""
        SELECT employee_code, name, phone, device_id, COALESCE(salary, 0)
        FROM employees
        WHERE employee_code = ?
        ORDER BY name ASC
        """, (employee_code.strip(),))
    else:
        cur.execute("""
        SELECT employee_code, name, phone, device_id, COALESCE(salary, 0)
        FROM employees
        ORDER BY name ASC
        """)

    rows = cur.fetchall()
    conn.close()

    employees = []
    for row in rows:
        employees.append({
            "employee_code": row[0],
            "name": row[1],
            "phone": row[2],
            "device_id": row[3],
            "salary": row[4] or 0
        })

    return employees


def get_attendance_for_month(employee_code, month):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        attendance_date,
        check_in_time,
        check_out_time,
        status
    FROM attendance
    WHERE employee_code = ?
    AND attendance_date LIKE ?
    ORDER BY attendance_date ASC
    """, (employee_code, month + "%"))

    rows = cur.fetchall()
    conn.close()

    attendance_rows = []
    totals = {
        "attendance_days": 0,
        "total_worked_hours": 0,
        "total_late_minutes": 0,
        "total_late_deduction": 0,
        "total_overtime_hours": 0,
        "total_overtime_amount": 0,
    }

    for row in rows:
        values = calculate_day_values(row[1], row[2])

        attendance_rows.append({
            "date": row[0],
            "check_in": row[1],
            "check_out": row[2],
            "worked_hours": values["worked_hours"],
            "late_minutes": values["late_minutes"],
            "late_deduction": values["late_deduction"],
            "overtime_hours": values["overtime_hours"],
            "overtime_amount": values["overtime_amount"],
            "status": row[3],
        })

        totals["attendance_days"] += 1
        totals["total_worked_hours"] += values["worked_hours"]
        totals["total_late_minutes"] += values["late_minutes"]
        totals["total_late_deduction"] += values["late_deduction"]
        totals["total_overtime_hours"] += values["overtime_hours"]
        totals["total_overtime_amount"] += values["overtime_amount"]

    totals["total_worked_hours"] = round(totals["total_worked_hours"], 2)
    totals["total_overtime_hours"] = round(totals["total_overtime_hours"], 2)
    totals["total_overtime_amount"] = round(totals["total_overtime_amount"], 2)

    return attendance_rows, totals


def get_finance_items_for_month(employee_code, month):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    SELECT id, employee_code, item_date, item_type, amount, reason, created_at
    FROM finance_items
    WHERE employee_code = ?
    AND item_date LIKE ?
    ORDER BY item_date ASC, id ASC
    """, (employee_code, month + "%"))

    rows = cur.fetchall()
    conn.close()

    items = []
    totals = {
        "advances_total": 0,
        "penalties_total": 0,
        "manual_deductions_total": 0,
        "bonuses_total": 0,
    }

    for row in rows:
        item_type = row[3]
        amount = float(row[4] or 0)

        if item_type == "advance":
            totals["advances_total"] += amount
        elif item_type == "penalty":
            totals["penalties_total"] += amount
        elif item_type == "deduction":
            totals["manual_deductions_total"] += amount
        elif item_type == "bonus":
            totals["bonuses_total"] += amount

        items.append({
            "id": row[0],
            "employee_code": row[1],
            "date": row[2],
            "type": item_type,
            "type_ar": finance_type_ar(item_type),
            "amount": amount,
            "reason": row[5],
            "created_at": row[6],
        })

    for key in totals:
        totals[key] = round(totals[key], 2)

    return items, totals


def build_payroll_report(month, employee_code=None):
    month = normalize_month(month)
    employees = get_employee_list(employee_code)

    report_rows = []
    summary = {
        "employees_count": 0,
        "base_salaries_total": 0,
        "bonuses_total": 0,
        "advances_total": 0,
        "penalties_total": 0,
        "manual_deductions_total": 0,
        "late_deductions_total": 0,
        "overtime_total": 0,
        "net_salaries_total": 0,
    }

    for employee in employees:
        code = employee["employee_code"]
        salary = float(employee["salary"] or 0)

        attendance_rows, attendance_totals = get_attendance_for_month(code, month)
        finance_items, finance_totals = get_finance_items_for_month(code, month)

        total_deductions = (
            finance_totals["advances_total"]
            + finance_totals["penalties_total"]
            + finance_totals["manual_deductions_total"]
            + attendance_totals["total_late_deduction"]
        )

        net_salary = (
            salary
            + finance_totals["bonuses_total"]
            + attendance_totals["total_overtime_amount"]
            - total_deductions
        )

        row = {
            "employee_code": code,
            "name": employee["name"],
            "phone": employee["phone"],
            "base_salary": round(salary, 2),
            "attendance_days": attendance_totals["attendance_days"],
            "worked_hours": attendance_totals["total_worked_hours"],
            "late_minutes": attendance_totals["total_late_minutes"],
            "late_deduction": attendance_totals["total_late_deduction"],
            "overtime_hours": attendance_totals["total_overtime_hours"],
            "overtime_amount": attendance_totals["total_overtime_amount"],
            "advances_total": finance_totals["advances_total"],
            "penalties_total": finance_totals["penalties_total"],
            "manual_deductions_total": finance_totals["manual_deductions_total"],
            "bonuses_total": finance_totals["bonuses_total"],
            "total_deductions": round(total_deductions, 2),
            "net_salary": round(net_salary, 2),
        }

        report_rows.append(row)

        summary["employees_count"] += 1
        summary["base_salaries_total"] += salary
        summary["bonuses_total"] += finance_totals["bonuses_total"]
        summary["advances_total"] += finance_totals["advances_total"]
        summary["penalties_total"] += finance_totals["penalties_total"]
        summary["manual_deductions_total"] += finance_totals["manual_deductions_total"]
        summary["late_deductions_total"] += attendance_totals["total_late_deduction"]
        summary["overtime_total"] += attendance_totals["total_overtime_amount"]
        summary["net_salaries_total"] += net_salary

    for key in summary:
        if key != "employees_count":
            summary[key] = round(summary[key], 2)

    return {
        "success": True,
        "month": month,
        "employee_code": employee_code,
        "summary": summary,
        "payroll": report_rows,
    }


@app.post("/employees/set-salary")
def set_employee_salary(data: SalaryUpdate):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    UPDATE employees
    SET salary = ?
    WHERE employee_code = ?
    """, (data.salary, data.employee_code.strip()))

    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "تم تحديث مرتب الموظف بنجاح"
    }


@app.post("/finance/items/create")
def create_finance_item(item: FinanceItemCreate):
    item_type = item.item_type.strip()

    if item_type not in VALID_FINANCE_TYPES:
        raise HTTPException(status_code=400, detail="نوع الحركة المالية غير صحيح")

    if item.amount <= 0:
        raise HTTPException(status_code=400, detail="المبلغ يجب أن يكون أكبر من صفر")

    item_date = item.item_date or today_date()

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    SELECT employee_code
    FROM employees
    WHERE employee_code = ?
    """, (item.employee_code.strip(),))

    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    cur.execute("""
    INSERT INTO finance_items
    (employee_code, item_date, item_type, amount, reason, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        item.employee_code.strip(),
        item_date,
        item_type,
        item.amount,
        item.reason,
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "تم إضافة الحركة المالية بنجاح"
    }


@app.get("/finance/items")
def list_finance_items(employee_code: str | None = None, month: str | None = None):
    month = normalize_month(month)

    conn = get_db()
    cur = conn.cursor()

    if employee_code:
        cur.execute("""
        SELECT f.id, f.employee_code, e.name, f.item_date, f.item_type, f.amount, f.reason, f.created_at
        FROM finance_items f
        LEFT JOIN employees e ON f.employee_code = e.employee_code
        WHERE f.employee_code = ?
        AND f.item_date LIKE ?
        ORDER BY f.item_date DESC, f.id DESC
        """, (employee_code.strip(), month + "%"))
    else:
        cur.execute("""
        SELECT f.id, f.employee_code, e.name, f.item_date, f.item_type, f.amount, f.reason, f.created_at
        FROM finance_items f
        LEFT JOIN employees e ON f.employee_code = e.employee_code
        WHERE f.item_date LIKE ?
        ORDER BY f.item_date DESC, f.id DESC
        """, (month + "%",))

    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        items.append({
            "id": row[0],
            "employee_code": row[1],
            "name": row[2],
            "date": row[3],
            "type": row[4],
            "type_ar": finance_type_ar(row[4]),
            "amount": row[5],
            "reason": row[6],
            "created_at": row[7],
        })

    return {
        "success": True,
        "month": month,
        "employee_code": employee_code,
        "items": items,
    }


@app.get("/finance/payroll")
def get_payroll(month: str | None = None, employee_code: str | None = None):
    return build_payroll_report(month, employee_code)


@app.get("/finance/employee-report")
def get_employee_full_report(employee_code: str, month: str | None = None):
    month = normalize_month(month)
    employees = get_employee_list(employee_code)

    if not employees:
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    employee = employees[0]
    attendance_rows, attendance_totals = get_attendance_for_month(employee_code.strip(), month)
    finance_items, finance_totals = get_finance_items_for_month(employee_code.strip(), month)
    payroll = build_payroll_report(month, employee_code.strip())

    return {
        "success": True,
        "month": month,
        "employee": employee,
        "attendance_summary": attendance_totals,
        "finance_summary": finance_totals,
        "salary_summary": payroll["payroll"][0] if payroll["payroll"] else {},
        "attendance": attendance_rows,
        "finance_items": finance_items,
    }


@app.get("/finance/export-payroll")
def export_payroll(month: str | None = None, employee_code: str | None = None):
    report = build_payroll_report(month, employee_code)
    month = report["month"]

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "كود الموظف",
        "اسم الموظف",
        "المرتب الأساسي",
        "أيام الحضور",
        "ساعات العمل",
        "دقائق التأخير",
        "خصم التأخير",
        "ساعات الإضافي",
        "قيمة الإضافي",
        "السلف",
        "الجزاءات",
        "خصومات أخرى",
        "المكافآت",
        "إجمالي الخصومات",
        "صافي المرتب"
    ])

    for row in report["payroll"]:
        writer.writerow([
            row["employee_code"],
            row["name"],
            row["base_salary"],
            row["attendance_days"],
            row["worked_hours"],
            row["late_minutes"],
            row["late_deduction"],
            row["overtime_hours"],
            row["overtime_amount"],
            row["advances_total"],
            row["penalties_total"],
            row["manual_deductions_total"],
            row["bonuses_total"],
            row["total_deductions"],
            row["net_salary"],
        ])

    output.seek(0)

    file_name = f"payroll_report_{month}.csv"

    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={file_name}"
        }
    )


@app.get("/payroll", response_class=HTMLResponse)
def payroll_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>المرتبات والسلف والجزاءات</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <style>
        body {
            font-family: Arial;
            background: #f3f4f6;
            padding: 20px;
            direction: rtl;
        }

        .box {
            background: white;
            padding: 20px;
            border-radius: 14px;
            box-shadow: 0 3px 12px rgba(0,0,0,0.12);
            margin-bottom: 20px;
        }

        h2, h3 {
            text-align: center;
        }

        input, select {
            width: 100%;
            padding: 12px;
            margin-bottom: 10px;
            border: 1px solid #ccc;
            border-radius: 8px;
            font-size: 15px;
            box-sizing: border-box;
        }

        button, a.action-link {
            padding: 12px 18px;
            color: white;
            border: none;
            border-radius: 8px;
            margin: 5px 0;
            font-size: 16px;
            cursor: pointer;
            display: block;
            width: 100%;
            box-sizing: border-box;
            text-align: center;
            text-decoration: none;
        }

        .blue { background: #2563eb; }
        .green { background: #16a34a; }
        .purple { background: #7c3aed; }
        .red { background: #dc2626; }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 15px;
        }

        .summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 10px;
        }

        .card {
            background: #eef2ff;
            padding: 12px;
            border-radius: 10px;
            text-align: center;
            font-weight: bold;
        }

        .msg {
            margin-top: 10px;
            padding: 10px;
            background: #eef2ff;
            border-radius: 8px;
            text-align: center;
        }

        .table-wrap {
            width: 100%;
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            background: white;
            margin-top: 10px;
            min-width: 1250px;
        }

        th {
            background: #111827;
            color: white;
            padding: 12px;
            white-space: nowrap;
        }

        td {
            padding: 11px;
            border-bottom: 1px solid #ddd;
            text-align: center;
            white-space: nowrap;
        }

        tr:nth-child(even) {
            background: #f9fafb;
        }

        .net {
            color: #16a34a;
            font-weight: bold;
        }
    </style>
</head>

<body>

<div class="box">
    <h2>المرتبات والسلف والجزاءات</h2>
    <a class="action-link blue" href="/admin">الرجوع إلى لوحة الإدارة</a>
    <a class="action-link purple" href="/employee-report">تقرير عامل مفصل</a>
</div>

<div class="grid">
    <div class="box">
        <h3>تحديث مرتب موظف</h3>
        <input id="salary_code" placeholder="كود الموظف">
        <input id="salary_value" type="number" placeholder="المرتب الشهري">
        <button class="green" onclick="updateSalary()">حفظ المرتب</button>
        <div class="msg" id="salary_msg">جاهز</div>
    </div>

    <div class="box">
        <h3>إضافة حركة مالية</h3>
        <input id="item_code" placeholder="كود الموظف">
        <input id="item_date" type="date">
        <select id="item_type">
            <option value="advance">سلفة</option>
            <option value="penalty">جزاء</option>
            <option value="deduction">خصم</option>
            <option value="bonus">مكافأة</option>
        </select>
        <input id="item_amount" type="number" placeholder="المبلغ">
        <input id="item_reason" placeholder="السبب / الملاحظة">
        <button class="green" onclick="addFinanceItem()">إضافة الحركة</button>
        <div class="msg" id="item_msg">جاهز</div>
    </div>
</div>

<div class="box">
    <h3>تقرير المرتبات الشهري</h3>
    <input id="report_month" type="month">
    <input id="report_code" placeholder="كود الموظف، اتركه فاضي لعرض كل الموظفين">
    <button class="blue" onclick="loadPayroll()">عرض التقرير</button>
    <button class="green" onclick="exportPayroll()">تصدير Excel</button>
</div>

<div class="box">
    <h3>ملخص التقرير</h3>
    <div class="summary">
        <div class="card" id="sum_employees">عدد الموظفين: 0</div>
        <div class="card" id="sum_base">إجمالي المرتبات: 0 ج</div>
        <div class="card" id="sum_bonus">المكافآت: 0 ج</div>
        <div class="card" id="sum_overtime">الإضافي: 0 ج</div>
        <div class="card" id="sum_advances">السلف: 0 ج</div>
        <div class="card" id="sum_penalties">الجزاءات: 0 ج</div>
        <div class="card" id="sum_deductions">الخصومات: 0 ج</div>
        <div class="card" id="sum_late">خصم التأخير: 0 ج</div>
        <div class="card" id="sum_net">صافي المرتبات: 0 ج</div>
    </div>
</div>

<div class="box">
    <h3>تفاصيل المرتبات</h3>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>كود الموظف</th>
                    <th>الاسم</th>
                    <th>المرتب الأساسي</th>
                    <th>أيام الحضور</th>
                    <th>ساعات العمل</th>
                    <th>دقائق التأخير</th>
                    <th>خصم التأخير</th>
                    <th>ساعات الإضافي</th>
                    <th>قيمة الإضافي</th>
                    <th>السلف</th>
                    <th>الجزاءات</th>
                    <th>خصومات أخرى</th>
                    <th>المكافآت</th>
                    <th>إجمالي الخصومات</th>
                    <th>صافي المرتب</th>
                </tr>
            </thead>
            <tbody id="payroll_body">
                <tr>
                    <td colspan="15">اضغط عرض التقرير</td>
                </tr>
            </tbody>
        </table>
    </div>
</div>

<script>
function setCurrentDates() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");

    document.getElementById("report_month").value = now.getFullYear() + "-" + month;
    document.getElementById("item_date").value = now.getFullYear() + "-" + month + "-" + day;
}

async function updateSalary() {
    const employee_code = document.getElementById("salary_code").value.trim();
    const salary = Number(document.getElementById("salary_value").value);
    const msg = document.getElementById("salary_msg");

    if (!employee_code || !salary) {
        msg.innerText = "اكتب كود الموظف والمرتب";
        return;
    }

    try {
        const res = await fetch("/employees/set-salary", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({employee_code, salary})
        });

        const data = await res.json();
        msg.innerText = data.message || data.detail || "تم";
        loadPayroll();

    } catch (e) {
        msg.innerText = "خطأ في الاتصال بالسيرفر";
    }
}

async function addFinanceItem() {
    const employee_code = document.getElementById("item_code").value.trim();
    const item_date = document.getElementById("item_date").value;
    const item_type = document.getElementById("item_type").value;
    const amount = Number(document.getElementById("item_amount").value);
    const reason = document.getElementById("item_reason").value.trim();
    const msg = document.getElementById("item_msg");

    if (!employee_code || !item_date || !amount) {
        msg.innerText = "اكتب كود الموظف والتاريخ والمبلغ";
        return;
    }

    try {
        const res = await fetch("/finance/items/create", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({employee_code, item_date, item_type, amount, reason})
        });

        const data = await res.json();
        msg.innerText = data.message || data.detail || "تم";
        loadPayroll();

    } catch (e) {
        msg.innerText = "خطأ في الاتصال بالسيرفر";
    }
}

async function loadPayroll() {
    const month = document.getElementById("report_month").value;
    const employee_code = document.getElementById("report_code").value.trim();
    const tbody = document.getElementById("payroll_body");

    let url = "/finance/payroll?month=" + encodeURIComponent(month);
    if (employee_code) {
        url += "&employee_code=" + encodeURIComponent(employee_code);
    }

    try {
        const res = await fetch(url);
        const data = await res.json();

        if (!res.ok) {
            tbody.innerHTML = "<tr><td colspan='15'>" + (data.detail || "حدث خطأ") + "</td></tr>";
            return;
        }

        const s = data.summary;
        document.getElementById("sum_employees").innerText = "عدد الموظفين: " + s.employees_count;
        document.getElementById("sum_base").innerText = "إجمالي المرتبات: " + s.base_salaries_total + " ج";
        document.getElementById("sum_bonus").innerText = "المكافآت: " + s.bonuses_total + " ج";
        document.getElementById("sum_overtime").innerText = "الإضافي: " + s.overtime_total + " ج";
        document.getElementById("sum_advances").innerText = "السلف: " + s.advances_total + " ج";
        document.getElementById("sum_penalties").innerText = "الجزاءات: " + s.penalties_total + " ج";
        document.getElementById("sum_deductions").innerText = "الخصومات: " + s.manual_deductions_total + " ج";
        document.getElementById("sum_late").innerText = "خصم التأخير: " + s.late_deductions_total + " ج";
        document.getElementById("sum_net").innerText = "صافي المرتبات: " + s.net_salaries_total + " ج";

        tbody.innerHTML = "";

        if (!data.payroll || data.payroll.length === 0) {
            tbody.innerHTML = "<tr><td colspan='15'>لا توجد بيانات</td></tr>";
            return;
        }

        data.payroll.forEach(row => {
            tbody.innerHTML += `
                <tr>
                    <td>${row.employee_code || ""}</td>
                    <td>${row.name || ""}</td>
                    <td>${row.base_salary || 0} ج</td>
                    <td>${row.attendance_days || 0}</td>
                    <td>${row.worked_hours || 0}</td>
                    <td>${row.late_minutes || 0}</td>
                    <td>${row.late_deduction || 0} ج</td>
                    <td>${row.overtime_hours || 0}</td>
                    <td>${row.overtime_amount || 0} ج</td>
                    <td>${row.advances_total || 0} ج</td>
                    <td>${row.penalties_total || 0} ج</td>
                    <td>${row.manual_deductions_total || 0} ج</td>
                    <td>${row.bonuses_total || 0} ج</td>
                    <td>${row.total_deductions || 0} ج</td>
                    <td class="net">${row.net_salary || 0} ج</td>
                </tr>
            `;
        });

    } catch (e) {
        tbody.innerHTML = "<tr><td colspan='15'>حدث خطأ في تحميل التقرير</td></tr>";
    }
}

function exportPayroll() {
    const month = document.getElementById("report_month").value;
    const employee_code = document.getElementById("report_code").value.trim();

    let url = "/finance/export-payroll?month=" + encodeURIComponent(month);
    if (employee_code) {
        url += "&employee_code=" + encodeURIComponent(employee_code);
    }

    window.open(url, "_blank");
}

setCurrentDates();
loadPayroll();
</script>

</body>
</html>
    """


@app.get("/employee-report", response_class=HTMLResponse)
def employee_report_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>تقرير العامل المفصل</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <style>
        body {
            font-family: Arial;
            background: #f3f4f6;
            padding: 20px;
            direction: rtl;
        }

        .box {
            background: white;
            padding: 20px;
            border-radius: 14px;
            box-shadow: 0 3px 12px rgba(0,0,0,0.12);
            margin-bottom: 20px;
        }

        h2, h3 {
            text-align: center;
        }

        input {
            width: 100%;
            padding: 12px;
            margin-bottom: 10px;
            border: 1px solid #ccc;
            border-radius: 8px;
            font-size: 15px;
            box-sizing: border-box;
        }

        button, a.action-link {
            padding: 12px 18px;
            color: white;
            border: none;
            border-radius: 8px;
            margin: 5px 0;
            font-size: 16px;
            cursor: pointer;
            display: block;
            width: 100%;
            box-sizing: border-box;
            text-align: center;
            text-decoration: none;
        }

        .blue { background: #2563eb; }
        .green { background: #16a34a; }
        .purple { background: #7c3aed; }

        .summary {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 10px;
        }

        .card {
            background: #eef2ff;
            padding: 12px;
            border-radius: 10px;
            text-align: center;
            font-weight: bold;
        }

        .table-wrap {
            width: 100%;
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            min-width: 950px;
            margin-top: 10px;
        }

        th {
            background: #111827;
            color: white;
            padding: 12px;
            white-space: nowrap;
        }

        td {
            padding: 11px;
            border-bottom: 1px solid #ddd;
            text-align: center;
            white-space: nowrap;
        }

        tr:nth-child(even) {
            background: #f9fafb;
        }
    </style>
</head>

<body>

<div class="box">
    <h2>تقرير العامل المفصل</h2>
    <a class="action-link blue" href="/admin">لوحة الإدارة</a>
    <a class="action-link purple" href="/payroll">تقرير المرتبات</a>
</div>

<div class="box">
    <h3>اختيار العامل</h3>
    <input id="employee_code" placeholder="كود الموظف">
    <input id="month" type="month">
    <button class="green" onclick="loadEmployeeReport()">عرض التقرير</button>
</div>

<div class="box">
    <h3 id="employee_title">بيانات العامل</h3>
    <div class="summary">
        <div class="card" id="base_salary">المرتب: 0 ج</div>
        <div class="card" id="attendance_days">أيام الحضور: 0</div>
        <div class="card" id="late_minutes">دقائق التأخير: 0</div>
        <div class="card" id="late_deduction">خصم التأخير: 0 ج</div>
        <div class="card" id="advances">السلف: 0 ج</div>
        <div class="card" id="penalties">الجزاءات: 0 ج</div>
        <div class="card" id="deductions">الخصومات: 0 ج</div>
        <div class="card" id="bonuses">المكافآت: 0 ج</div>
        <div class="card" id="overtime">الإضافي: 0 ج</div>
        <div class="card" id="net_salary">صافي المرتب: 0 ج</div>
    </div>
</div>

<div class="box">
    <h3>حضور وتأخير العامل</h3>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>التاريخ</th>
                    <th>الحضور</th>
                    <th>الانصراف</th>
                    <th>ساعات العمل</th>
                    <th>دقائق التأخير</th>
                    <th>خصم التأخير</th>
                    <th>ساعات الإضافي</th>
                    <th>قيمة الإضافي</th>
                    <th>الحالة</th>
                </tr>
            </thead>
            <tbody id="attendance_body">
                <tr><td colspan="9">اكتب كود الموظف واضغط عرض التقرير</td></tr>
            </tbody>
        </table>
    </div>
</div>

<div class="box">
    <h3>السلف والخصومات والمكافآت والجزاءات</h3>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>التاريخ</th>
                    <th>النوع</th>
                    <th>المبلغ</th>
                    <th>السبب</th>
                </tr>
            </thead>
            <tbody id="finance_body">
                <tr><td colspan="4">اكتب كود الموظف واضغط عرض التقرير</td></tr>
            </tbody>
        </table>
    </div>
</div>

<script>
function setCurrentMonth() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    document.getElementById("month").value = now.getFullYear() + "-" + month;
}

async function loadEmployeeReport() {
    const employee_code = document.getElementById("employee_code").value.trim();
    const month = document.getElementById("month").value;

    if (!employee_code || !month) {
        alert("اكتب كود الموظف واختار الشهر");
        return;
    }

    try {
        const res = await fetch("/finance/employee-report?employee_code=" + encodeURIComponent(employee_code) + "&month=" + encodeURIComponent(month));
        const data = await res.json();

        if (!res.ok) {
            alert(data.detail || "حدث خطأ");
            return;
        }

        const employee = data.employee;
        const salary = data.salary_summary;
        const attendance = data.attendance_summary;
        const finance = data.finance_summary;

        document.getElementById("employee_title").innerText = "تقرير: " + employee.name + " - كود " + employee.employee_code;
        document.getElementById("base_salary").innerText = "المرتب: " + (salary.base_salary || 0) + " ج";
        document.getElementById("attendance_days").innerText = "أيام الحضور: " + (attendance.attendance_days || 0);
        document.getElementById("late_minutes").innerText = "دقائق التأخير: " + (attendance.total_late_minutes || 0);
        document.getElementById("late_deduction").innerText = "خصم التأخير: " + (attendance.total_late_deduction || 0) + " ج";
        document.getElementById("advances").innerText = "السلف: " + (finance.advances_total || 0) + " ج";
        document.getElementById("penalties").innerText = "الجزاءات: " + (finance.penalties_total || 0) + " ج";
        document.getElementById("deductions").innerText = "الخصومات: " + (finance.manual_deductions_total || 0) + " ج";
        document.getElementById("bonuses").innerText = "المكافآت: " + (finance.bonuses_total || 0) + " ج";
        document.getElementById("overtime").innerText = "الإضافي: " + (attendance.total_overtime_amount || 0) + " ج";
        document.getElementById("net_salary").innerText = "صافي المرتب: " + (salary.net_salary || 0) + " ج";

        const attendanceBody = document.getElementById("attendance_body");
        attendanceBody.innerHTML = "";

        if (!data.attendance || data.attendance.length === 0) {
            attendanceBody.innerHTML = "<tr><td colspan='9'>لا يوجد حضور في هذا الشهر</td></tr>";
        } else {
            data.attendance.forEach(row => {
                attendanceBody.innerHTML += `
                    <tr>
                        <td>${row.date || ""}</td>
                        <td>${row.check_in || ""}</td>
                        <td>${row.check_out || ""}</td>
                        <td>${row.worked_hours || 0}</td>
                        <td>${row.late_minutes || 0}</td>
                        <td>${row.late_deduction || 0} ج</td>
                        <td>${row.overtime_hours || 0}</td>
                        <td>${row.overtime_amount || 0} ج</td>
                        <td>${row.status || ""}</td>
                    </tr>
                `;
            });
        }

        const financeBody = document.getElementById("finance_body");
        financeBody.innerHTML = "";

        if (!data.finance_items || data.finance_items.length === 0) {
            financeBody.innerHTML = "<tr><td colspan='4'>لا توجد حركات مالية في هذا الشهر</td></tr>";
        } else {
            data.finance_items.forEach(row => {
                financeBody.innerHTML += `
                    <tr>
                        <td>${row.date || ""}</td>
                        <td>${row.type_ar || ""}</td>
                        <td>${row.amount || 0} ج</td>
                        <td>${row.reason || ""}</td>
                    </tr>
                `;
            });
        }

    } catch (e) {
        alert("خطأ في الاتصال بالسيرفر");
    }
}

setCurrentMonth();
</script>

</body>
</html>
    """
