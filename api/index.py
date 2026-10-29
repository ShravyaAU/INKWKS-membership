from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv
import httpx

load_dotenv()

app = FastAPI(title="INK|WKS Portal API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    conn = psycopg2.connect(
        os.getenv("NEON_DATABASE_URL"),
        cursor_factory=RealDictCursor
    )
    try:
        yield conn
    finally:
        conn.close()

class GoogleToken(BaseModel):
    token: str

class SessionUpdate(BaseModel):
    admin_id: str
    target_email: str
    action: str

@app.get("/api/health")
def health_check():
    return {"status": "ok"}

@app.post("/api/auth/google")
async def verify_google_sso(token_data: GoogleToken, db=Depends(get_db)):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={token_data.token}")
    
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")
        
    user_info = response.json()
    email = user_info.get("email", "")
    
    if not email.endswith("@asu.edu"):
        raise HTTPException(status_code=403, detail="Only @asu.edu emails are permitted.")
        
    cursor = db.cursor()
    cursor.execute("SELECT id, role, orientation_completed FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()
    
    if not user:
        cursor.execute(
            "INSERT INTO users (email, role) VALUES (%s, 'student') RETURNING id, role, orientation_completed", 
            (email,)
        )
        user = cursor.fetchone()
        db.commit()
     
    user["email"] = email
    return {"message": "Authentication successful", "user": user}

@app.get("/api/memberships/{user_id}")
def get_membership_status(user_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        SELECT sessions_remaining, expiration_date 
        FROM memberships 
        WHERE user_id = %s AND status = 'active' AND expiration_date > CURRENT_TIMESTAMP
        ORDER BY expiration_date DESC LIMIT 1
    """, (user_id,))
    
    membership = cursor.fetchone()
    if membership:
        return {"has_pass": True, "sessions_remaining": membership["sessions_remaining"], "expiration_date": str(membership["expiration_date"])}
    return {"has_pass": False, "sessions_remaining": 0}

# --- NEW: Admin Session Slot Management ---
class StudioSessionCreate(BaseModel):
    admin_id: str
    session_time: str
    available_seats: int

@app.post("/api/admin/sessionslots")
def create_session_slot(req: StudioSessionCreate, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (req.admin_id,))
    if cursor.fetchone().get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    cursor.execute("""
        INSERT INTO studio_sessions (session_time, available_seats) 
        VALUES (%s, %s)
    """, (req.session_time, req.available_seats))
    db.commit()
    return {"message": "Session slot published."}

@app.get("/api/sessionslots/available")
def get_available_session_slots(db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        SELECT id, session_time, available_seats 
        FROM studio_sessions 
        WHERE session_time > CURRENT_TIMESTAMP AND available_seats > 0
        ORDER BY session_time ASC
    """)
    return cursor.fetchall()

# --- Updated Appointment Booking ---
class AppointmentRequest(BaseModel):
    user_id: str
    session_slot_id: str

@app.post("/api/appointments")
def book_appointment(req: AppointmentRequest, db=Depends(get_db)):
    cursor = db.cursor()
    
    # 1. Verify Pass
    cursor.execute("""
        SELECT id, sessions_remaining 
        FROM memberships 
        WHERE user_id = %s AND status = 'active' AND expiration_date > CURRENT_TIMESTAMP
        ORDER BY expiration_date DESC LIMIT 1
    """, (req.user_id,))
    membership = cursor.fetchone()
    if not membership or membership['sessions_remaining'] <= 0:
        raise HTTPException(status_code=400, detail="No active sessions remaining on your pass.")
        
    # 2. Verify Session Slot has seats
    cursor.execute("SELECT session_time, available_seats FROM studio_sessions WHERE id = %s", (req.session_slot_id,))
    slot = cursor.fetchone()
    if not slot or slot['available_seats'] <= 0:
        raise HTTPException(status_code=400, detail="This session is full or unavailable.")
        
    # 3. Book it, reduce slot capacity, reduce pass sessions
    cursor.execute("INSERT INTO appointments (user_id, appointment_time, status) VALUES (%s, %s, 'booked')", (req.user_id, slot['session_time']))
    cursor.execute("UPDATE studio_sessions SET available_seats = available_seats - 1 WHERE id = %s", (req.session_slot_id,))
    cursor.execute("UPDATE memberships SET sessions_remaining = sessions_remaining - 1 WHERE id = %s RETURNING sessions_remaining", (membership['id'],))
    
    updated_sessions = cursor.fetchone()['sessions_remaining']
    db.commit()
    return {"message": "Session booked successfully!", "sessions_remaining": updated_sessions}

@app.get("/api/appointments/{user_id}")
def get_appointments(user_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT appointment_time, status FROM appointments WHERE user_id = %s ORDER BY appointment_time ASC", (user_id,))
    return cursor.fetchall()

# --- Role & Faculty Requests ---
class RoleUpdateRequest(BaseModel):
    admin_id: str
    target_email: str
    new_role: str

@app.put("/api/admin/roles")
def update_user_role(req: RoleUpdateRequest, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (req.admin_id,))
    if cursor.fetchone().get('role') != 'admin':
        raise HTTPException(status_code=403, detail="Unauthorized")
    cursor.execute("UPDATE users SET role = %s WHERE email = %s RETURNING id", (req.new_role, req.target_email))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="User not found.")
    db.commit()
    return {"message": "Role updated"}

class FacultyRequestModel(BaseModel):
    user_id: str
    department: str
    course_code: str
    category: str
    requested_date: str
    participants: int
    cost_center: str
    specifications: str

@app.post("/api/faculty/requests")
def submit_faculty_request(req: FacultyRequestModel, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        INSERT INTO faculty_requests (user_id, department, course_code, category, requested_date, participants, cost_center, specifications) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (req.user_id, req.department, req.course_code, req.category, req.requested_date, req.participants, req.cost_center, req.specifications))
    db.commit()
    return {"message": "Faculty request submitted successfully!"}

@app.get("/api/faculty/requests/{user_id}")
def get_faculty_requests(user_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT course_code, category, requested_date, status FROM faculty_requests WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    return cursor.fetchall()

# --- Workshops ---
@app.get("/api/workshops/available")
def get_available_workshops(db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT id, title, instructor, workshop_date, available_seats, price FROM workshops WHERE workshop_date > CURRENT_TIMESTAMP AND available_seats > 0 ORDER BY workshop_date ASC")
    return cursor.fetchall()

class WorkshopRegistration(BaseModel):
    user_id: str
    workshop_id: str

@app.post("/api/workshops/register")
def register_workshop(req: WorkshopRegistration, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT available_seats FROM workshops WHERE id = %s", (req.workshop_id,))
    workshop = cursor.fetchone()
    if not workshop or workshop['available_seats'] <= 0:
        raise HTTPException(status_code=400, detail="Workshop full.")
    cursor.execute("INSERT INTO workshop_registrations (workshop_id, user_id) VALUES (%s, %s)", (req.workshop_id, req.user_id))
    cursor.execute("UPDATE workshops SET available_seats = available_seats - 1 WHERE id = %s", (req.workshop_id,))
    db.commit()
    return {"message": "Successfully registered!"}

@app.get("/api/workshops/my-registrations/{user_id}")
def get_my_workshops(user_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("""
        SELECT w.title, w.workshop_date, r.status 
        FROM workshop_registrations r
        JOIN workshops w ON r.workshop_id = w.id
        WHERE r.user_id = %s ORDER BY w.workshop_date ASC
    """, (user_id,))
    return cursor.fetchall()

class WorkshopCreate(BaseModel):
    admin_id: str
    title: str
    instructor: str
    price: float
    available_seats: int
    workshop_date: str

@app.post("/api/admin/workshops")
def create_admin_workshop(req: WorkshopCreate, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (req.admin_id,))
    if cursor.fetchone().get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    cursor.execute("INSERT INTO workshops (title, instructor, price, available_seats, workshop_date) VALUES (%s, %s, %s, %s, %s)", (req.title, req.instructor, req.price, req.available_seats, req.workshop_date))
    db.commit()
    return {"message": "Workshop created."}

@app.delete("/api/workshops/{workshop_id}")
def delete_admin_workshop(workshop_id: str, admin_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (admin_id,))
    if cursor.fetchone().get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    cursor.execute("DELETE FROM workshop_registrations WHERE workshop_id = %s", (workshop_id,))
    cursor.execute("DELETE FROM workshops WHERE id = %s", (workshop_id,))
    db.commit()
    return {"message": "Workshop deleted."}

# --- Admin Dashboard ---
@app.put("/api/admin/sessions")
def update_session_count(req: SessionUpdate, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (req.admin_id,))
    if cursor.fetchone().get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    cursor.execute("SELECT id FROM users WHERE email = %s", (req.target_email,))
    target_user = cursor.fetchone()
    if not target_user: raise HTTPException(status_code=404, detail="Student not found.")
    cursor.execute("SELECT id, sessions_remaining FROM memberships WHERE user_id = %s AND status = 'active'", (target_user['id'],))
    membership = cursor.fetchone()
    if not membership: raise HTTPException(status_code=400, detail="No active pass.")
    
    current_sessions = membership['sessions_remaining']
    if req.action == "deduct":
        if current_sessions <= 0: raise HTTPException(status_code=400, detail="0 sessions left.")
        new_sessions = current_sessions - 1
    elif req.action == "restore":
        new_sessions = current_sessions + 1
        
    cursor.execute("UPDATE memberships SET sessions_remaining = %s WHERE id = %s", (new_sessions, membership['id']))
    db.commit()
    return {"message": "Success", "sessions_remaining": new_sessions}

@app.get("/api/admin/schedule")
def get_admin_schedule(admin_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (admin_id,))
    if cursor.fetchone().get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    cursor.execute("""
        SELECT a.id, a.appointment_time, a.status, u.email 
        FROM appointments a JOIN users u ON a.user_id = u.id
        WHERE a.appointment_time > CURRENT_TIMESTAMP ORDER BY a.appointment_time ASC
    """)
    return cursor.fetchall()

@app.get("/api/admin/inbox")
def get_admin_inbox(admin_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (admin_id,))
    user_check = cursor.fetchone()
    if not user_check or user_check.get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    
    try:
        # ILIKE '%pending%' catches 'pending', 'PENDING', or 'PENDING REVIEW'
        cursor.execute("""
            SELECT id, course_code as title, requested_date as date, participants, 'faculty' as type 
            FROM faculty_requests 
            WHERE status ILIKE '%pending%' OR status IS NULL
        """)
        faculty = cursor.fetchall()
        return {"faculty": faculty, "clubs": []}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

class RequestReview(BaseModel):
    admin_id: str
    request_type: str
    request_id: str
    new_status: str

@app.put("/api/admin/inbox/review")
def review_inbox_request(req: RequestReview, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (req.admin_id,))
    if cursor.fetchone().get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    if req.request_type == 'faculty':
        cursor.execute("UPDATE faculty_requests SET status = %s WHERE id = %s", (req.new_status, req.request_id))
    db.commit()
    return {"message": "Request updated."}

@app.get("/api/admin/roster/{item_type}/{item_id}")
def get_roster(item_type: str, item_id: str, admin_id: str, db=Depends(get_db)):
    cursor = db.cursor()
    cursor.execute("SELECT role FROM users WHERE id = %s", (admin_id,))
    if cursor.fetchone().get('role') != 'admin': raise HTTPException(status_code=403, detail="Unauthorized")
    if item_type == "workshop":
        cursor.execute("SELECT u.email FROM workshop_registrations r JOIN users u ON r.user_id = u.id WHERE r.workshop_id = %s", (item_id,))
    else:
        cursor.execute("SELECT u.email FROM appointments a JOIN users u ON a.user_id = u.id WHERE a.id = %s", (item_id,))
    return [r['email'] for r in cursor.fetchall()]