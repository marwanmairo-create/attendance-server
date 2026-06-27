from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from pydantic import BaseModel
import sqlite3
from datetime import datetime, date
import io
import csv
import calendar
import base64

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
LATE_DEDUCTION = 0
OVERTIME_HOUR_RATE = 50


def now_time():
    return datetime.now().strftime("%H:%M:%S")


def today_date():
    return date.today().isoformat()


def is_late(check_in_time: str):
    # تم إلغاء حساب التأخير نهائيًا بناءً على طلبك.
    # لا يوجد تأخير في بداية اليوم ولا في نهاية اليوم.
    return False, 0


def calculate_day_values(check_in_time, check_out_time):
    # التأخير والخصم ملغيين نهائيًا.
    late_minutes = 0
    late_deduction = 0
    worked_hours = 0
    overtime_hours = 0
    overtime_amount = 0
    net_amount = 0

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

    net_amount = overtime_amount

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
    status = "حاضر"

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
<div class="box"><h2>لوحة إدارة الحضور</h2><a class="monthly-link" href="/employees-db">قاعدة بيانات الموظفين</a><a class="monthly-link" href="/monthly">فتح التقرير الشهري</a><a class="monthly-link" href="/payroll">المرتبات والسلف والجزاءات</a><a class="monthly-link" href="/employee-report">تقرير عامل مفصل</a></div>
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



# =========================
# Extended Employee Database
# =========================

def ensure_employee_profile_schema():
    conn = get_db()
    cur = conn.cursor()

    columns = {
        "national_id": "TEXT",
        "address": "TEXT",
        "job_title": "TEXT",
        "department": "TEXT",
        "hire_date": "TEXT",
        "salary_type": "TEXT DEFAULT 'monthly'",
        "work_start": "TEXT DEFAULT '11:00'",
        "work_end": "TEXT DEFAULT '23:00'",
        "grace_minutes": "INTEGER DEFAULT 5",
        "late_deduction_amount": "REAL DEFAULT 50",
        "overtime_hour_rate": "REAL DEFAULT 50",
        "employee_status": "TEXT DEFAULT 'active'",
        "notes": "TEXT"
    }

    for column, definition in columns.items():
        try:
            cur.execute(f"ALTER TABLE employees ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


ensure_employee_profile_schema()


class EmployeeFullCreate(BaseModel):
    employee_code: str
    name: str
    phone: str | None = None
    password: str
    device_id: str | None = None
    salary: float | None = 0
    national_id: str | None = None
    address: str | None = None
    job_title: str | None = None
    department: str | None = None
    hire_date: str | None = None
    salary_type: str | None = "monthly"
    work_start: str | None = "11:00"
    work_end: str | None = "23:00"
    grace_minutes: int | None = 5
    late_deduction_amount: float | None = 50
    overtime_hour_rate: float | None = 50
    employee_status: str | None = "active"
    notes: str | None = None


class EmployeeProfileUpdate(BaseModel):
    employee_code: str
    name: str | None = None
    phone: str | None = None
    password: str | None = None
    device_id: str | None = None
    salary: float | None = None
    national_id: str | None = None
    address: str | None = None
    job_title: str | None = None
    department: str | None = None
    hire_date: str | None = None
    salary_type: str | None = None
    work_start: str | None = None
    work_end: str | None = None
    grace_minutes: int | None = None
    late_deduction_amount: float | None = None
    overtime_hour_rate: float | None = None
    employee_status: str | None = None
    notes: str | None = None


def employee_row_to_dict(row):
    return {
        "employee_code": row[0],
        "name": row[1],
        "phone": row[2],
        "password": row[3],
        "device_id": row[4],
        "salary": row[5] or 0,
        "national_id": row[6],
        "address": row[7],
        "job_title": row[8],
        "department": row[9],
        "hire_date": row[10],
        "salary_type": row[11] or "monthly",
        "work_start": row[12] or "11:00",
        "work_end": row[13] or "23:00",
        "grace_minutes": row[14] if row[14] is not None else 5,
        "late_deduction_amount": row[15] if row[15] is not None else 50,
        "overtime_hour_rate": row[16] if row[16] is not None else 50,
        "employee_status": row[17] or "active",
        "notes": row[18],
    }


@app.get("/employees/full")
def get_full_employees(employee_code: str | None = None):
    ensure_employee_profile_schema()

    conn = get_db()
    cur = conn.cursor()

    select_sql = """
    SELECT
        employee_code,
        name,
        phone,
        password,
        device_id,
        COALESCE(salary, 0),
        national_id,
        address,
        job_title,
        department,
        hire_date,
        COALESCE(salary_type, 'monthly'),
        COALESCE(work_start, '11:00'),
        COALESCE(work_end, '23:00'),
        COALESCE(grace_minutes, 5),
        COALESCE(late_deduction_amount, 50),
        COALESCE(overtime_hour_rate, 50),
        COALESCE(employee_status, 'active'),
        notes
    FROM employees
    """

    if employee_code:
        cur.execute(select_sql + " WHERE employee_code = ? ORDER BY name ASC", (employee_code.strip(),))
    else:
        cur.execute(select_sql + " ORDER BY name ASC")

    rows = cur.fetchall()
    conn.close()

    return {
        "success": True,
        "employees": [employee_row_to_dict(row) for row in rows]
    }


@app.post("/employees/full-create")
def create_full_employee(employee: EmployeeFullCreate):
    ensure_employee_profile_schema()

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
        INSERT INTO employees
        (
            employee_code,
            name,
            phone,
            password,
            device_id,
            salary,
            national_id,
            address,
            job_title,
            department,
            hire_date,
            salary_type,
            work_start,
            work_end,
            grace_minutes,
            late_deduction_amount,
            overtime_hour_rate,
            employee_status,
            notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            employee.employee_code.strip(),
            employee.name.strip(),
            employee.phone.strip() if employee.phone else None,
            employee.password.strip(),
            employee.device_id.strip() if employee.device_id else None,
            employee.salary or 0,
            employee.national_id.strip() if employee.national_id else None,
            employee.address.strip() if employee.address else None,
            employee.job_title.strip() if employee.job_title else None,
            employee.department.strip() if employee.department else None,
            employee.hire_date.strip() if employee.hire_date else None,
            employee.salary_type or "monthly",
            employee.work_start or "11:00",
            employee.work_end or "23:00",
            employee.grace_minutes if employee.grace_minutes is not None else 5,
            employee.late_deduction_amount if employee.late_deduction_amount is not None else 50,
            employee.overtime_hour_rate if employee.overtime_hour_rate is not None else 50,
            employee.employee_status or "active",
            employee.notes.strip() if employee.notes else None,
        ))

        conn.commit()

    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="كود الموظف موجود بالفعل")

    conn.close()

    return {
        "success": True,
        "message": "تم إنشاء ملف الموظف بنجاح"
    }


@app.post("/employees/update-profile")
def update_employee_profile(data: EmployeeProfileUpdate):
    ensure_employee_profile_schema()

    allowed_fields = [
        "name",
        "phone",
        "password",
        "device_id",
        "salary",
        "national_id",
        "address",
        "job_title",
        "department",
        "hire_date",
        "salary_type",
        "work_start",
        "work_end",
        "grace_minutes",
        "late_deduction_amount",
        "overtime_hour_rate",
        "employee_status",
        "notes",
    ]

    payload = data.model_dump(exclude_unset=True)
    employee_code = payload.pop("employee_code").strip()

    updates = []
    values = []

    for field in allowed_fields:
        if field in payload:
            value = payload[field]
            if isinstance(value, str):
                value = value.strip()
            if value == "":
                value = None
            updates.append(f"{field} = ?")
            values.append(value)

    if not updates:
        raise HTTPException(status_code=400, detail="لا توجد بيانات لتحديثها")

    values.append(employee_code)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(f"""
    UPDATE employees
    SET {", ".join(updates)}
    WHERE employee_code = ?
    """, values)

    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "تم تحديث بيانات الموظف بنجاح"
    }


# =========================
# Employee Database Page
# =========================

@app.get("/employees-db", response_class=HTMLResponse)
def employees_database_page():
    return """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<title>قاعدة بيانات الموظفين</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{font-family:Arial;background:#f3f4f6;padding:20px;direction:rtl}
.box{background:white;padding:20px;border-radius:14px;box-shadow:0 3px 12px rgba(0,0,0,.12);margin-bottom:20px}
h2,h3{text-align:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
input,select,textarea{width:100%;padding:12px;border:1px solid #ccc;border-radius:8px;font-size:15px;box-sizing:border-box}
textarea{min-height:80px}
button,a.btn{padding:12px 18px;color:white;border:none;border-radius:8px;margin:5px 0;font-size:16px;cursor:pointer;text-decoration:none;display:inline-block;text-align:center}
.add{background:#16a34a}.update{background:#2563eb}.refresh{background:#7c3aed}.back{background:#111827}.msg{margin-top:10px;padding:12px;background:#eef2ff;border-radius:8px;text-align:center;font-weight:bold}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:1350px}
th{background:#111827;color:white;padding:10px;white-space:nowrap}
td{padding:9px;border-bottom:1px solid #ddd;text-align:center;white-space:nowrap}
tr:nth-child(even){background:#f9fafb}
.small{font-size:12px;color:#555;text-align:center}
</style>
</head>
<body>

<div class="box">
<h2>قاعدة بيانات الموظفين</h2>
<div style="text-align:center">
<a class="btn back" href="/admin">الرجوع للوحة الإدارة</a>
<a class="btn refresh" href="/payroll">المرتبات والسلف والجزاءات</a>
<a class="btn refresh" href="/employee-report">تقرير عامل مفصل</a>
</div>
<p class="small">هنا يتم تسجيل الملف الكامل لكل موظف: الراتب الأساسي، الوظيفة، القسم، مواعيد العمل، التأخير، الإضافي، وحالة الموظف.</p>
</div>

<div class="box">
<h3>إضافة / تعديل ملف موظف</h3>

<div class="grid">
<input id="employee_code" placeholder="كود الموظف">
<input id="name" placeholder="اسم الموظف">
<input id="phone" placeholder="رقم الهاتف">
<input id="password" placeholder="كلمة السر">
<input id="device_id" placeholder="كود الجهاز اختياري">
<input id="salary" type="number" placeholder="الراتب الأساسي">
<input id="national_id" placeholder="الرقم القومي">
<input id="address" placeholder="العنوان">
<input id="job_title" placeholder="الوظيفة">
<input id="department" placeholder="القسم / الفرع">
<input id="hire_date" type="date" placeholder="تاريخ التعيين">
<select id="salary_type">
<option value="monthly">شهري</option>
<option value="daily">يومي</option>
</select>
<input id="work_start" placeholder="ميعاد الحضور مثال 11:00" value="11:00">
<input id="work_end" placeholder="ميعاد الانصراف مثال 23:00" value="23:00">
<input id="grace_minutes" type="number" placeholder="سماح التأخير بالدقائق" value="5">
<input id="late_deduction_amount" type="number" placeholder="خصم التأخير" value="50">
<input id="overtime_hour_rate" type="number" placeholder="قيمة ساعة الإضافي" value="50">
<select id="employee_status">
<option value="active">نشط</option>
<option value="inactive">موقوف</option>
</select>
</div>

<div style="margin-top:10px">
<textarea id="notes" placeholder="ملاحظات"></textarea>
</div>

<button class="add" onclick="createEmployee()">إضافة موظف جديد</button>
<button class="update" onclick="updateEmployee()">تعديل بيانات الموظف</button>
<button class="refresh" onclick="loadEmployees()">تحديث القائمة</button>

<div class="msg" id="msg">جاهز</div>
</div>

<div class="box">
<h3>ملفات الموظفين</h3>
<div class="table-wrap">
<table>
<thead>
<tr>
<th>اختيار</th>
<th>الكود</th>
<th>الاسم</th>
<th>الهاتف</th>
<th>الراتب الأساسي</th>
<th>الوظيفة</th>
<th>القسم</th>
<th>تاريخ التعيين</th>
<th>نوع الراتب</th>
<th>الحضور</th>
<th>الانصراف</th>
<th>السماح</th>
<th>خصم التأخير</th>
<th>ساعة الإضافي</th>
<th>الحالة</th>
<th>كود الجهاز</th>
<th>الرقم القومي</th>
<th>العنوان</th>
<th>ملاحظات</th>
</tr>
</thead>
<tbody id="employees_body">
<tr><td colspan="19">جاري تحميل الموظفين...</td></tr>
</tbody>
</table>
</div>
</div>

<script>
function val(id){return document.getElementById(id).value.trim();}
function setVal(id,v){document.getElementById(id).value = v || "";}
function msg(t){document.getElementById("msg").innerText=t;}

function payload(){
    return {
        employee_code: val("employee_code"),
        name: val("name"),
        phone: val("phone") || null,
        password: val("password"),
        device_id: val("device_id") || null,
        salary: Number(val("salary") || 0),
        national_id: val("national_id") || null,
        address: val("address") || null,
        job_title: val("job_title") || null,
        department: val("department") || null,
        hire_date: val("hire_date") || null,
        salary_type: val("salary_type") || "monthly",
        work_start: val("work_start") || "11:00",
        work_end: val("work_end") || "23:00",
        grace_minutes: Number(val("grace_minutes") || 5),
        late_deduction_amount: Number(val("late_deduction_amount") || 50),
        overtime_hour_rate: Number(val("overtime_hour_rate") || 50),
        employee_status: val("employee_status") || "active",
        notes: val("notes") || null
    };
}

async function createEmployee(){
    const p = payload();
    if(!p.employee_code || !p.name || !p.password){msg("اكتب الكود والاسم وكلمة السر");return;}
    try{
        const res = await fetch("/employees/full-create",{
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify(p)
        });
        const data = await res.json();
        if(!res.ok){msg(data.detail || "حدث خطأ");return;}
        msg(data.message);
        loadEmployees();
    }catch(e){msg("خطأ في الاتصال بالسيرفر");}
}

async function updateEmployee(){
    const p = payload();
    if(!p.employee_code){msg("اكتب كود الموظف المراد تعديله");return;}
    try{
        const res = await fetch("/employees/update-profile",{
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify(p)
        });
        const data = await res.json();
        if(!res.ok){msg(data.detail || "حدث خطأ");return;}
        msg(data.message);
        loadEmployees();
    }catch(e){msg("خطأ في الاتصال بالسيرفر");}
}

function selectEmployee(e){
    setVal("employee_code", e.employee_code);
    setVal("name", e.name);
    setVal("phone", e.phone);
    setVal("password", e.password);
    setVal("device_id", e.device_id);
    setVal("salary", e.salary);
    setVal("national_id", e.national_id);
    setVal("address", e.address);
    setVal("job_title", e.job_title);
    setVal("department", e.department);
    setVal("hire_date", e.hire_date);
    setVal("salary_type", e.salary_type || "monthly");
    setVal("work_start", e.work_start || "11:00");
    setVal("work_end", e.work_end || "23:00");
    setVal("grace_minutes", e.grace_minutes || 5);
    setVal("late_deduction_amount", e.late_deduction_amount || 50);
    setVal("overtime_hour_rate", e.overtime_hour_rate || 50);
    setVal("employee_status", e.employee_status || "active");
    setVal("notes", e.notes);
    msg("تم اختيار الموظف للتعديل: " + e.name);
    window.scrollTo({top:0, behavior:"smooth"});
}

async function loadEmployees(){
    const tbody = document.getElementById("employees_body");
    try{
        const res = await fetch("/employees/full");
        const data = await res.json();
        tbody.innerHTML="";
        if(!data.employees || data.employees.length===0){
            tbody.innerHTML="<tr><td colspan='19'>لا يوجد موظفين</td></tr>";
            return;
        }
        data.employees.forEach(e=>{
            const safe = JSON.stringify(e).replaceAll("'", "&#39;");
            tbody.innerHTML += `
            <tr>
                <td><button class="update" onclick='selectEmployee(${safe})'>اختيار</button></td>
                <td>${e.employee_code || ""}</td>
                <td>${e.name || ""}</td>
                <td>${e.phone || ""}</td>
                <td>${e.salary || 0} ج</td>
                <td>${e.job_title || ""}</td>
                <td>${e.department || ""}</td>
                <td>${e.hire_date || ""}</td>
                <td>${e.salary_type || ""}</td>
                <td>${e.work_start || ""}</td>
                <td>${e.work_end || ""}</td>
                <td>${e.grace_minutes || 0}</td>
                <td>${e.late_deduction_amount || 0} ج</td>
                <td>${e.overtime_hour_rate || 0} ج</td>
                <td>${e.employee_status || ""}</td>
                <td>${e.device_id || ""}</td>
                <td>${e.national_id || ""}</td>
                <td>${e.address || ""}</td>
                <td>${e.notes || ""}</td>
            </tr>`;
        });
    }catch(e){
        tbody.innerHTML="<tr><td colspan='19'>حدث خطأ في تحميل الموظفين</td></tr>";
    }
}

loadEmployees();
</script>
</body>
</html>
    """



# =========================
# Mobile Employee Dashboard / Report
# =========================

def is_weekly_holiday(date_text):
    try:
        d = datetime.strptime(date_text, "%Y-%m-%d").date()
        # Python: Monday=0 ... Sunday=6
        return d.weekday() == 6
    except Exception:
        return False


def get_attendance_calendar_for_employee(employee_code, month):
    month = normalize_month(month)
    year = int(month[:4])
    month_number = int(month[5:7])
    days_count = calendar.monthrange(year, month_number)[1]

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    SELECT attendance_date, check_in_time, check_out_time, status
    FROM attendance
    WHERE employee_code = ?
    AND attendance_date LIKE ?
    ORDER BY attendance_date ASC
    """, (employee_code.strip(), month + "%"))
    rows = cur.fetchall()
    conn.close()

    by_date = {}
    for row in rows:
        values = calculate_day_values(row[1], row[2])
        by_date[row[0]] = {
            "date": row[0],
            "check_in": row[1],
            "check_out": row[2],
            "worked_hours": values["worked_hours"],
            "late_minutes": 0,
            "late_deduction": 0,
            "overtime_hours": values["overtime_hours"],
            "overtime_amount": values["overtime_amount"],
            "status": row[3] or "حاضر",
            "is_holiday": is_weekly_holiday(row[0]),
        }

    calendar_rows = []
    totals = {
        "attendance_days": 0,
        "weekly_holidays": 0,
        "not_registered_days": 0,
        "total_worked_hours": 0,
        "total_late_minutes": 0,
        "total_late_deduction": 0,
        "total_overtime_hours": 0,
        "total_overtime_amount": 0,
    }

    for day in range(1, days_count + 1):
        date_text = f"{month}-{day:02d}"

        if date_text in by_date:
            row = by_date[date_text]
            totals["attendance_days"] += 1
            totals["total_worked_hours"] += row["worked_hours"]
            totals["total_overtime_hours"] += row["overtime_hours"]
            totals["total_overtime_amount"] += row["overtime_amount"]
            calendar_rows.append(row)
            continue

        if is_weekly_holiday(date_text):
            totals["weekly_holidays"] += 1
            calendar_rows.append({
                "date": date_text,
                "check_in": None,
                "check_out": None,
                "worked_hours": 0,
                "late_minutes": 0,
                "late_deduction": 0,
                "overtime_hours": 0,
                "overtime_amount": 0,
                "status": "إجازة أسبوعية",
                "is_holiday": True,
            })
        else:
            totals["not_registered_days"] += 1
            calendar_rows.append({
                "date": date_text,
                "check_in": None,
                "check_out": None,
                "worked_hours": 0,
                "late_minutes": 0,
                "late_deduction": 0,
                "overtime_hours": 0,
                "overtime_amount": 0,
                "status": "لم يسجل",
                "is_holiday": False,
            })

    totals["total_worked_hours"] = round(totals["total_worked_hours"], 2)
    totals["total_overtime_hours"] = round(totals["total_overtime_hours"], 2)
    totals["total_overtime_amount"] = round(totals["total_overtime_amount"], 2)

    return calendar_rows, totals


@app.get("/mobile/employee-dashboard")
def mobile_employee_dashboard(employee_code: str, month: str | None = None):
    month = normalize_month(month)
    employees = get_employee_list(employee_code)

    if not employees:
        raise HTTPException(status_code=404, detail="الموظف غير موجود")

    employee = employees[0]
    attendance_rows, attendance_totals = get_attendance_calendar_for_employee(employee_code.strip(), month)
    finance_items, finance_totals = get_finance_items_for_month(employee_code.strip(), month)

    total_deductions = (
        finance_totals["advances_total"]
        + finance_totals["penalties_total"]
        + finance_totals["manual_deductions_total"]
    )

    net_salary = (
        float(employee.get("salary") or 0)
        + finance_totals["bonuses_total"]
        + attendance_totals["total_overtime_amount"]
        - total_deductions
    )

    salary_summary = {
        "base_salary": round(float(employee.get("salary") or 0), 2),
        "attendance_days": attendance_totals["attendance_days"],
        "weekly_holidays": attendance_totals["weekly_holidays"],
        "worked_hours": attendance_totals["total_worked_hours"],
        "late_minutes": 0,
        "late_deduction": 0,
        "overtime_hours": attendance_totals["total_overtime_hours"],
        "overtime_amount": attendance_totals["total_overtime_amount"],
        "advances_total": finance_totals["advances_total"],
        "penalties_total": finance_totals["penalties_total"],
        "manual_deductions_total": finance_totals["manual_deductions_total"],
        "bonuses_total": finance_totals["bonuses_total"],
        "total_deductions": round(total_deductions, 2),
        "net_salary": round(net_salary, 2),
    }

    return {
        "success": True,
        "month": month,
        "employee": employee,
        "attendance_summary": attendance_totals,
        "finance_summary": finance_totals,
        "salary_summary": salary_summary,
        "attendance": attendance_rows,
        "finance_items": finance_items,
    }



# =========================
# iPhone / iOS Web App - El Batal
# =========================

IPHONE_ICON_BASE64 = "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"

IPHONE_WEBAPP_HTML = r"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#b91c1c">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="البطل">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/iphone/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/iphone/icon-512.png">
  <title>البطل</title>
  <style>
    :root{
      --red:#b91c1c;
      --red2:#ef4444;
      --dark:#111827;
      --muted:#6b7280;
      --bg:#f3f4f6;
      --card:#ffffff;
      --green:#16a34a;
      --orange:#f59e0b;
      --blue:#2563eb;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Tahoma,Arial,sans-serif;
      background:linear-gradient(180deg,#fff 0%,var(--bg) 45%,#e5e7eb 100%);
      color:var(--dark);
      min-height:100vh;
      padding-bottom:calc(20px + env(safe-area-inset-bottom));
    }
    .top{
      background:linear-gradient(135deg,#7f1d1d,var(--red),#ef4444);
      color:#fff;
      padding:calc(18px + env(safe-area-inset-top)) 18px 24px;
      border-bottom-left-radius:28px;
      border-bottom-right-radius:28px;
      box-shadow:0 8px 22px rgba(0,0,0,.18);
      text-align:center;
      position:sticky;
      top:0;
      z-index:5;
    }
    .top img{width:78px;height:78px;border-radius:20px;background:#fff;padding:6px;object-fit:contain}
    .top h1{margin:8px 0 2px;font-size:28px}
    .top p{margin:0;opacity:.9}
    .wrap{padding:16px;max-width:760px;margin:0 auto}
    .card{
      background:var(--card);
      border-radius:18px;
      box-shadow:0 8px 24px rgba(17,24,39,.08);
      padding:16px;
      margin:14px 0;
      border:1px solid rgba(17,24,39,.06);
    }
    label{display:block;font-weight:700;margin:8px 0 6px}
    input,select{
      width:100%;
      border:1px solid #d1d5db;
      border-radius:13px;
      padding:14px;
      font-size:16px;
      background:#fff;
      outline:none;
    }
    input:focus{border-color:var(--red);box-shadow:0 0 0 3px rgba(185,28,28,.12)}
    button{
      border:0;
      border-radius:14px;
      padding:14px 16px;
      font-size:16px;
      font-weight:800;
      color:#fff;
      background:var(--red);
      cursor:pointer;
      width:100%;
      margin-top:10px;
      min-height:50px;
    }
    button.secondary{background:#374151}
    button.green{background:var(--green)}
    button.blue{background:var(--blue)}
    button.orange{background:var(--orange)}
    button.outline{background:#fff;color:var(--dark);border:1px solid #d1d5db}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .msg{
      padding:12px;
      border-radius:14px;
      background:#eef2ff;
      color:#1e1b4b;
      text-align:center;
      font-weight:700;
      margin:12px 0;
      white-space:pre-wrap;
    }
    .hidden{display:none!important}
    .row{
      display:flex;
      justify-content:space-between;
      gap:10px;
      border-bottom:1px solid #f1f5f9;
      padding:10px 0;
      align-items:center;
    }
    .row:last-child{border-bottom:0}
    .row b{color:#111827}
    .row span{color:#374151;text-align:left}
    .section-title{font-size:20px;font-weight:900;margin:20px 2px 8px}
    .pill{display:inline-block;padding:5px 10px;border-radius:999px;background:#fee2e2;color:#7f1d1d;font-size:13px;font-weight:800}
    .list-item{
      border:1px solid #e5e7eb;
      border-radius:15px;
      padding:12px;
      margin:8px 0;
      background:#fff;
    }
    .list-head{display:flex;justify-content:space-between;gap:8px;font-weight:900}
    .small{font-size:13px;color:var(--muted);line-height:1.7}
    .install{
      background:#fff7ed;
      color:#7c2d12;
      border:1px solid #fed7aa;
      border-radius:16px;
      padding:12px;
      font-size:14px;
      line-height:1.7;
    }
    .tabs{
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:8px;
      margin-top:10px;
    }
    .tabs button{
      margin:0;
      background:#fff;
      color:#111827;
      border:1px solid #d1d5db;
      min-height:44px;
    }
    .tabs button.active{
      background:var(--red);
      color:#fff;
      border-color:var(--red);
    }
  </style>
</head>
<body>
  <div class="top">
    <img src="/iphone/icon-512.png" alt="البطل">
    <h1>البطل</h1>
    <p>تسجيل حضور الموظفين</p>
  </div>

  <div class="wrap">

    <div id="installBox" class="install">
      على الآيفون: افتح الصفحة من Safari ثم اضغط Share وبعدها Add to Home Screen عشان تظهر كأبلكيشن باسم البطل.
    </div>

    <div id="loginCard" class="card">
      <h2 style="margin-top:0">تسجيل الدخول</h2>

      <label>رابط السيرفر</label>
      <input id="serverUrl" value="" placeholder="https://attendance-server-production-5d8a.up.railway.app">

      <label>كود الموظف</label>
      <input id="employeeCode" placeholder="مثال: 1001">

      <label>كلمة السر</label>
      <input id="password" type="password" placeholder="كلمة السر">

      <label>كود الجهاز اختياري</label>
      <input id="deviceId" placeholder="اتركه فارغ لو مش مستخدم">

      <button onclick="login()">تسجيل الدخول</button>
      <div id="loginMsg" class="msg">جاهز</div>
    </div>

    <div id="homeCard" class="hidden">
      <div class="card">
        <div style="display:flex;align-items:center;gap:12px">
          <img src="/iphone/icon-512.png" style="width:58px;height:58px;border-radius:16px;border:1px solid #eee;background:#fff;padding:4px">
          <div>
            <div style="font-size:21px;font-weight:900" id="empName">الموظف</div>
            <div class="small">كود الموظف: <span id="empCodeText"></span></div>
          </div>
        </div>
        <div class="tabs">
          <button id="tabToday" class="active" onclick="showTab('today')">اليوم</button>
          <button id="tabReport" onclick="showTab('report')">تقريري</button>
        </div>
      </div>

      <div id="todayPage">
        <div class="grid">
          <button class="green" onclick="sendAttendance('in')">تسجيل حضور</button>
          <button onclick="sendAttendance('out')">تسجيل انصراف</button>
        </div>

        <div id="mainMsg" class="msg">جاهز</div>

        <div class="card">
          <h2 style="margin-top:0">بيانات اليوم</h2>
          <div class="row"><b>الحالة</b><span id="todayStatus">-</span></div>
          <div class="row"><b>وقت الحضور</b><span id="todayIn">-</span></div>
          <div class="row"><b>وقت الانصراف</b><span id="todayOut">-</span></div>
          <div class="row"><b>ساعات العمل</b><span id="todayHours">0</span></div>
          <div class="row"><b>التأخير</b><span>ملغي</span></div>
          <div class="row"><b>الإضافي</b><span id="todayOvertime">0</span></div>
        </div>
      </div>

      <div id="reportPage" class="hidden">
        <div class="card">
          <label>شهر التقرير</label>
          <input id="reportMonth" type="month">
          <button class="blue" onclick="loadReport()">تحميل التقرير</button>
        </div>

        <div id="reportMsg" class="msg">اختر الشهر واضغط تحميل التقرير</div>

        <div class="card">
          <h2 style="margin-top:0">ملخص المرتب</h2>
          <div id="salarySummary"></div>
        </div>

        <div class="section-title">الحضور والانصراف يوميًا</div>
        <div id="attendanceList"></div>

        <div class="section-title">السلف والجزاءات والخصومات والمكافآت</div>
        <div id="financeList"></div>
      </div>

      <button class="secondary" onclick="logout()">تسجيل خروج</button>
    </div>
  </div>

<script>
  const defaultServer = location.origin;

  function cleanBase(){
    return (localStorage.serverUrl || defaultServer).replace(/\/$/,'');
  }

  function setMsg(id, text){ document.getElementById(id).textContent = text; }

  function money(v){
    const n = Number(v || 0);
    return (Number.isInteger(n) ? n.toString() : n.toFixed(2)) + ' ج';
  }

  function val(v){ return (v === null || v === undefined || v === '') ? '-' : v; }

  function currentMonth(){
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0');
  }

  async function apiGet(path){
    const res = await fetch(cleanBase() + path);
    const data = await res.json().catch(()=>({}));
    if(!res.ok){ throw new Error(data.detail || 'حدث خطأ في السيرفر'); }
    return data;
  }

  async function apiPost(path, body){
    const res = await fetch(cleanBase() + path, {
      method:'POST',
      headers:{'Content-Type':'application/json; charset=utf-8'},
      body:JSON.stringify(body)
    });
    const data = await res.json().catch(()=>({}));
    if(!res.ok){ throw new Error(data.detail || 'حدث خطأ في السيرفر'); }
    return data;
  }

  function init(){
    document.getElementById('serverUrl').value = localStorage.serverUrl || defaultServer;
    document.getElementById('employeeCode').value = localStorage.employeeCode || '';
    document.getElementById('deviceId').value = localStorage.deviceId || '';
    document.getElementById('reportMonth').value = currentMonth();

    if(localStorage.employeeCode){
      openHome();
      loadToday();
    }
  }

  async function login(){
    const server = document.getElementById('serverUrl').value.trim();
    const code = document.getElementById('employeeCode').value.trim();
    const password = document.getElementById('password').value.trim();
    const device = document.getElementById('deviceId').value.trim();

    if(!server || !code || !password){
      setMsg('loginMsg','اكتب رابط السيرفر وكود الموظف وكلمة السر');
      return;
    }

    try{
      setMsg('loginMsg','جاري تسجيل الدخول...');
      localStorage.serverUrl = server;
      const data = await apiPost('/login', {
        employee_code: code,
        password: password,
        device_id: device || null
      });
      localStorage.employeeCode = code;
      localStorage.deviceId = device;
      localStorage.employeeName = data.name || code;
      openHome();
      await loadToday();
    }catch(e){
      setMsg('loginMsg', e.message);
    }
  }

  function openHome(){
    document.getElementById('loginCard').classList.add('hidden');
    document.getElementById('homeCard').classList.remove('hidden');
    document.getElementById('empName').textContent = localStorage.employeeName || 'الموظف';
    document.getElementById('empCodeText').textContent = localStorage.employeeCode || '';
  }

  function logout(){
    localStorage.removeItem('employeeCode');
    localStorage.removeItem('employeeName');
    localStorage.removeItem('deviceId');
    location.reload();
  }

  function showTab(tab){
    const today = tab === 'today';
    document.getElementById('todayPage').classList.toggle('hidden', !today);
    document.getElementById('reportPage').classList.toggle('hidden', today);
    document.getElementById('tabToday').classList.toggle('active', today);
    document.getElementById('tabReport').classList.toggle('active', !today);
    if(!today){ loadReport(); }
  }

  async function getLocation(){
    return new Promise(resolve => {
      if(!navigator.geolocation){
        resolve({lat:null,lng:null});
        return;
      }
      navigator.geolocation.getCurrentPosition(
        pos => resolve({lat:pos.coords.latitude, lng:pos.coords.longitude}),
        () => resolve({lat:null,lng:null}),
        {enableHighAccuracy:true, timeout:9000, maximumAge:0}
      );
    });
  }

  async function sendAttendance(type){
    const endpoint = type === 'in' ? '/attendance/check-in' : '/attendance/check-out';
    try{
      setMsg('mainMsg','جاري تحديد الموقع...');
      const loc = await getLocation();
      setMsg('mainMsg','جاري إرسال التسجيل...');
      const data = await apiPost(endpoint, {
        employee_code: localStorage.employeeCode,
        device_id: localStorage.deviceId || null,
        lat: loc.lat,
        lng: loc.lng
      });
      setMsg('mainMsg', data.message || 'تمت العملية بنجاح');
      await loadToday();
    }catch(e){
      setMsg('mainMsg', e.message);
    }
  }

  async function loadToday(){
    try{
      const data = await apiGet('/attendance/today');
      const list = data.attendance || [];
      const row = list.find(x => String(x.employee_code) === String(localStorage.employeeCode));
      document.getElementById('todayStatus').textContent = row ? val(row.status) : 'لا يوجد تسجيل اليوم';
      document.getElementById('todayIn').textContent = row ? val(row.check_in) : '-';
      document.getElementById('todayOut').textContent = row ? val(row.check_out) : '-';
      document.getElementById('todayHours').textContent = row ? val(row.worked_hours) : '0';
      document.getElementById('todayOvertime').textContent = row ? val(row.overtime_hours) : '0';
    }catch(e){
      setMsg('mainMsg', e.message);
    }
  }

  async function loadReport(){
    const month = document.getElementById('reportMonth').value || currentMonth();
    try{
      setMsg('reportMsg','جاري تحميل التقرير...');
      const data = await apiGet('/mobile/employee-dashboard?employee_code=' + encodeURIComponent(localStorage.employeeCode) + '&month=' + encodeURIComponent(month));

      const s = data.salary_summary || {};
      document.getElementById('salarySummary').innerHTML = [
        ['الراتب الأساسي', money(s.base_salary)],
        ['السلف', money(s.advances_total)],
        ['الجزاءات', money(s.penalties_total)],
        ['الخصومات', money(s.manual_deductions_total)],
        ['المكافآت', money(s.bonuses_total)],
        ['الإضافي', money(s.overtime_amount)],
        ['خصم التأخير', 'ملغي'],
        ['صافي المرتب', money(s.net_salary)]
      ].map(r => `<div class="row"><b>${r[0]}</b><span>${r[1]}</span></div>`).join('');

      const attendance = data.attendance || [];
      document.getElementById('attendanceList').innerHTML = attendance.length ? attendance.map(x => `
        <div class="list-item">
          <div class="list-head">
            <span>${val(x.date)}</span>
            <span class="pill">${val(x.status)}</span>
          </div>
          <div class="small">
            حضور: ${val(x.check_in)} | انصراف: ${val(x.check_out)}<br>
            ساعات: ${val(x.worked_hours)} | إضافي: ${val(x.overtime_hours)}
          </div>
        </div>
      `).join('') : '<div class="msg">لا توجد بيانات حضور</div>';

      const finance = data.finance_items || [];
      document.getElementById('financeList').innerHTML = finance.length ? finance.map(x => `
        <div class="list-item">
          <div class="list-head">
            <span>${val(x.type_ar)}</span>
            <span>${money(x.amount)}</span>
          </div>
          <div class="small">${val(x.date)}<br>${val(x.reason)}</div>
        </div>
      `).join('') : '<div class="msg">لا توجد حركات مالية في هذا الشهر</div>';

      setMsg('reportMsg','تم تحميل التقرير');
    }catch(e){
      setMsg('reportMsg', e.message);
    }
  }

  if('serviceWorker' in navigator){
    navigator.serviceWorker.register('/iphone/sw.js').catch(()=>{});
  }

  init();
</script>
</body>
</html>
"""

IPHONE_MANIFEST_JSON = """
{
  "name": "البطل",
  "short_name": "البطل",
  "description": "تطبيق حضور وانصراف موظفي البطل",
  "start_url": "/iphone",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait",
  "dir": "rtl",
  "lang": "ar",
  "background_color": "#ffffff",
  "theme_color": "#b91c1c",
  "icons": [
    {
      "src": "/iphone/icon-192.png",
      "sizes": "192x192",
      "type": "image/png"
    },
    {
      "src": "/iphone/icon-512.png",
      "sizes": "512x512",
      "type": "image/png"
    }
  ]
}
"""

IPHONE_SW_JS = """
self.addEventListener('install', event => {
  event.waitUntil(caches.open('elbatal-v1').then(cache => cache.addAll(['/iphone', '/iphone/icon-512.png'])));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
"""


@app.get("/iphone", response_class=HTMLResponse)
def iphone_webapp():
    return HTMLResponse(IPHONE_WEBAPP_HTML)


@app.get("/iphone/manifest.webmanifest")
def iphone_manifest():
    return Response(IPHONE_MANIFEST_JSON, media_type="application/manifest+json")


@app.get("/iphone/sw.js")
def iphone_service_worker():
    return Response(IPHONE_SW_JS, media_type="application/javascript")


@app.get("/iphone/icon-512.png")
def iphone_icon_512():
    return Response(base64.b64decode(IPHONE_ICON_BASE64), media_type="image/png")


@app.get("/iphone/icon-192.png")
def iphone_icon_192():
    return Response(base64.b64decode(IPHONE_ICON_BASE64), media_type="image/png")
