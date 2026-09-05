from typing import Optional, List
from datetime import datetime, date
from sqlmodel import SQLModel, Field, Relationship, Column, JSON
from sqlalchemy import Text, UniqueConstraint
import enum

class UserRole(str, enum.Enum):
    general_admin = "general_admin"
    church_admin = "church_admin"
    data_officer = "data_officer"
    member = "member"

class ChurchLevel(str, enum.Enum):
    global_church = "global"
    country = "country"
    state = "state"
    group = "group"
    district = "district"

class MemberStatus(str, enum.Enum):
    member = "member"
    worker = "worker"
    leader = "leader"
    pastor = "pastor"

class SexType(str, enum.Enum):
    brother = "brother"
    sister = "sister"

class AgeCategory(str, enum.Enum):
    child = "child"           # 1-15
    youth = "youth"           # 16-20
    campus = "campus"         # 21-40 young adult/campus
    adult = "adult"           # 30-100

class Confession(str, enum.Enum):
    saved = "saved"
    unsaved = "unsaved"
    backslidden = "backslidden"
    restored = "restored"

class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    discontinued = "discontinued"

class ChurchUnit(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("code", name="uq_church_code"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True)
    name: str
    level: ChurchLevel
    parent_id: Optional[int] = Field(default=None, foreign_key="churchunit.id")
    global_code: Optional[str] = None
    country_code: Optional[str] = None
    state_code: Optional[str] = None
    group_code: Optional[str] = None
    district_code: Optional[str] = None
    country_name: Optional[str] = None
    state_name: Optional[str] = None
    doctrine: Optional[str] = Field(default=None, sa_column=Column(Text))
    activity_days: Optional[str] = None
    owner_name: Optional[str] = None
    resident_pastor: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    # Remittance accounts (shown on member dashboard)
    tithe_account_name: Optional[str] = None
    tithe_account_number: Optional[str] = None
    tithe_bank_name: Optional[str] = None
    offering_account_name: Optional[str] = None
    offering_account_number: Optional[str] = None
    offering_bank_name: Optional[str] = None
    pastor_phone: Optional[str] = None
    pastor_email: Optional[str] = None
    weekly_activities_note: Optional[str] = Field(default=None, sa_column=Column(Text))
    logo_url: Optional[str] = None  # Global church brand logo for page + home adverts
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    approval_status: str = Field(default="pending")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    members: List["ChurchMember"] = Relationship(back_populates="church")
    stats: List["WeeklyStat"] = Relationship(back_populates="church")
    programs: List["SpecialProgram"] = Relationship(back_populates="church")

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str
    full_name: str
    role: UserRole = Field(default=UserRole.member)
    church_id: Optional[int] = Field(default=None, foreign_key="churchunit.id")
    member_id: Optional[int] = Field(default=None, foreign_key="churchmember.id")
    can_enter_stats: bool = Field(default=False)  # designated for weekly attendance
    can_create_churches: bool = Field(default=False)  # GA grants: create child churches
    can_approve_members: bool = Field(default=False)  # GA grants: approve member registrations
    can_see_member_count: bool = Field(default=False)
    can_broadcast: bool = Field(default=False)
    is_sample_account: bool = Field(default=False)
    sample_started_at: Optional[datetime] = None  # first login for time-bound sample  # approved to broadcast to members
    can_view_church_dashboard: bool = Field(default=False)
    can_manage_focus_groups: bool = Field(default=False)  # create focus groups / message them  # sub-admin grants church dashboard to member  # see district member totals
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None

class ChurchMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id")  # district (or unit) they belong to
    global_church_id: Optional[int] = None
    country_church_id: Optional[int] = None
    state_church_id: Optional[int] = None
    group_church_id: Optional[int] = None
    full_name: str
    sex: Optional[str] = None              # brother / sister
    age_category: Optional[str] = None     # child, youth, campus, adult
    confession: Optional[str] = None       # saved, unsaved, backslidden, restored
    member_since: Optional[date] = None
    prayer_request: Optional[str] = Field(default=None, sa_column=Column(Text))
    address: Optional[str] = None
    whatsapp: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = Field(default=None, index=True)
    profile_pic: Optional[str] = None
    status: str = Field(default="member")  # member, worker, leader, pastor
    worker_type: Optional[str] = None
    leader_type: Optional[str] = None
    custom_title: Optional[str] = None  # editable title from sub-admin
    approval_status: str = Field(default="pending")  # pending, approved, rejected, discontinued
    discontinue_requested: bool = Field(default=False)
    is_active: bool = Field(default=True)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    church: Optional[ChurchUnit] = Relationship(back_populates="members")

class WeeklyStat(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id")
    week_start: date
    adult_male: int = 0
    adult_female: int = 0
    children_boys: int = 0
    children_girls: int = 0
    youth_male: int = 0
    youth_female: int = 0
    offering: float = 0.0
    tithe: float = 0.0
    donation: float = 0.0
    special_program_attendance: int = 0
    newcomers: int = 0
    converts: int = 0
    counseling: int = 0
    members_in_need: int = 0
    notes: Optional[str] = None
    entered_by: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    church: Optional[ChurchUnit] = Relationship(back_populates="stats")

class SpecialProgram(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id")  # creating unit
    title: str
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    program_date: Optional[date] = None
    location: Optional[str] = None
    broadcast_to: str = Field(default="district")  # district | group | state | country | global
    created_by: Optional[int] = None
    is_active: bool = Field(default=True)
    # Home showcase: global church requests → general admin approves with time limit
    request_home_display: bool = Field(default=False)  # Global church asked to show on public home
    featured_on_home: bool = Field(default=False)  # General Admin approved for landing
    home_display_hours: Optional[float] = Field(default=None)  # approved duration (≤ 24)
    home_display_starts_at: Optional[datetime] = Field(default=None)
    home_display_ends_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    church: Optional[ChurchUnit] = Relationship(back_populates="programs")
    photos: List["ProgramPhoto"] = Relationship(back_populates="program")
    videos: List["ProgramVideo"] = Relationship(back_populates="program")

class ProgramPhoto(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    program_id: int = Field(foreign_key="specialprogram.id")
    file_path: str
    caption: Optional[str] = None
    uploaded_by: Optional[int] = None  # church admin only
    created_at: datetime = Field(default_factory=datetime.utcnow)

    program: Optional[SpecialProgram] = Relationship(back_populates="photos")
    likes: List["PhotoLike"] = Relationship(back_populates="photo")
    comments: List["PhotoComment"] = Relationship(back_populates="photo")

class PhotoLike(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    photo_id: int = Field(foreign_key="programphoto.id")
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    photo: Optional[ProgramPhoto] = Relationship(back_populates="likes")

class PhotoComment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    photo_id: int = Field(foreign_key="programphoto.id")
    user_id: int = Field(foreign_key="user.id")
    body: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)

    photo: Optional[ProgramPhoto] = Relationship(back_populates="comments")

class ActivityLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: Optional[int] = None
    user_id: Optional[int] = None
    action: str
    detail: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

class SpecialProject(SQLModel, table=True):
    """Fundraising / special project with collection tracking for admin dashboard."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id")
    title: str
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    target_amount: float = Field(default=0.0)
    account_name: Optional[str] = None
    account_number: Optional[str] = None
    bank_name: Optional[str] = None
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class SpecialProjectContribution(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="specialproject.id")
    amount: float = Field(default=0.0)
    contributor_name: Optional[str] = None
    note: Optional[str] = None
    recorded_by: Optional[int] = None
    contributed_at: datetime = Field(default_factory=datetime.utcnow)


class AdminMessage(SQLModel, table=True):
    """Church admin messages to all or selected members."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    sender_id: int = Field(foreign_key="user.id")
    subject: str
    body: str = Field(sa_column=Column(Text))
    # JSON list of member ids; empty / null = all approved members in tree
    recipient_member_ids: Optional[str] = Field(default=None, sa_column=Column(Text))
    send_to_all: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MemberMessageReceipt(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="adminmessage.id", index=True)
    member_id: int = Field(foreign_key="churchmember.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    is_read: bool = Field(default=False)
    read_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FocusGroup(SQLModel, table=True):
    """Admin-created group of selected members for targeted messages."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    name: str
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class FocusGroupMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="focusgroup.id", index=True)
    member_id: int = Field(foreign_key="churchmember.id", index=True)
    added_at: datetime = Field(default_factory=datetime.utcnow)

class FocusGroupMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int = Field(foreign_key="focusgroup.id", index=True)
    sender_id: int = Field(foreign_key="user.id")
    body: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class FocusGroupMessageComment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="focusgroupmessage.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    body: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Testimony(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    member_id: Optional[int] = Field(default=None, foreign_key="churchmember.id")
    user_id: int = Field(foreign_key="user.id")
    title: str
    body: str = Field(sa_column=Column(Text))
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class TestimonyLike(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    testimony_id: int = Field(foreign_key="testimony.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)

class TestimonyComment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    testimony_id: int = Field(foreign_key="testimony.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    body: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class HeartNeed(SQLModel, table=True):
    """Heart to Heart — member shares a need; others can support."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    member_id: Optional[int] = Field(default=None, foreign_key="churchmember.id")
    user_id: int = Field(foreign_key="user.id")
    title: str
    situation: str = Field(sa_column=Column(Text))
    amount_requested: float = Field(default=0.0)
    amount_raised: float = Field(default=0.0)
    status: str = Field(default="open")  # open, helped, closed
    receipt_affirmed: bool = Field(default=False)
    receipt_note: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)

class HeartDonation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    need_id: Optional[int] = Field(default=None, foreign_key="heartneed.id")  # null = general pool
    donor_user_id: int = Field(foreign_key="user.id")
    amount: float = Field(default=0.0)
    note: Optional[str] = None
    is_anonymous: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class HeartDistribution(SQLModel, table=True):
    """Admin records how Heart to Heart funds were given out."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    need_id: Optional[int] = Field(default=None, foreign_key="heartneed.id")
    beneficiary_member_id: Optional[int] = Field(default=None, foreign_key="churchmember.id")
    amount: float = Field(default=0.0)
    note: Optional[str] = Field(default=None, sa_column=Column(Text))
    recorded_by: Optional[int] = Field(default=None, foreign_key="user.id")
    beneficiary_affirmed: bool = Field(default=False)
    affirmed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PaymentSettings(SQLModel, table=True):
    """Platform payment details filled by General Admin — shown to all church admins."""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(default="Platform subscription / service fee")
    instructions: Optional[str] = Field(default=None, sa_column=Column(Text))
    bank_name: Optional[str] = None
    account_name: Optional[str] = None
    account_number: Optional[str] = None
    currency: str = Field(default="NGN")
    default_amount: float = Field(default=0.0)
    other_details: Optional[str] = Field(default=None, sa_column=Column(Text))
    is_active: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: Optional[int] = None


class ChurchPayment(SQLModel, table=True):
    """Invoice / payment record for a church; General Admin confirms and issues receipt."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    amount: float = Field(default=0.0)
    currency: str = Field(default="NGN")
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    status: str = Field(default="pending")  # pending | confirmed | cancelled
    reference: Optional[str] = Field(default=None, index=True)
    receipt_number: Optional[str] = None
    receipt_note: Optional[str] = Field(default=None, sa_column=Column(Text))
    paid_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    confirmed_by: Optional[int] = None
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class HomeVideo(SQLModel, table=True):
    """Global church short video for public home page — expires after 24 hours."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)
    title: Optional[str] = None
    caption: Optional[str] = Field(default=None, sa_column=Column(Text))
    file_path: str
    uploaded_by: Optional[int] = None
    duration_seconds: Optional[int] = None  # client-reported; target ~30s
    is_active: bool = Field(default=True)
    starts_at: datetime = Field(default_factory=datetime.utcnow)
    ends_at: Optional[datetime] = None  # starts_at + 24h
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProgramVideo(SQLModel, table=True):
    """Short video attached to a special program (staff upload)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    program_id: int = Field(foreign_key="specialprogram.id", index=True)
    file_path: str
    caption: Optional[str] = None
    uploaded_by: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    program: Optional[SpecialProgram] = Relationship(back_populates="videos")


class YoutubeChannelLink(SQLModel, table=True):
    """YouTube channel/video links for home page — Global church or platform admin."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: Optional[int] = Field(default=None, foreign_key="churchunit.id", index=True)  # null = General Admin
    owner_type: str = Field(default="global_church")  # global_church | general_admin
    title: str = Field(default="YouTube")
    youtube_url: str
    youtube_video_id: Optional[str] = None  # extracted for embed
    is_approved: bool = Field(default=False)
    is_active: bool = Field(default=True)
    submitted_by: Optional[int] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MusicLink(SQLModel, table=True):
    """YouTube worship tracks managed by General Admin."""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    youtube_id: str = Field(index=True)
    is_active: bool = Field(default=True)
    sort_order: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DistrictMessage(SQLModel, table=True):
    """Messages between district members and district admin/pastor."""
    id: Optional[int] = Field(default=None, primary_key=True)
    church_id: int = Field(foreign_key="churchunit.id", index=True)  # district unit
    sender_user_id: int = Field(foreign_key="user.id", index=True)
    subject: Optional[str] = None
    body: str = Field(sa_column=Column(Text))
    is_broadcast: bool = Field(default=False)
    # null = all members in district; else comma-separated user ids
    recipient_user_ids: Optional[str] = Field(default=None, sa_column=Column(Text))
    # target: members | admin | pastor
    to_role: str = Field(default="members")  # members | admin | pastor
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MessageRead(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="districtmessage.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    read_at: datetime = Field(default_factory=datetime.utcnow)


class SubscriptionSettings(SQLModel, table=True):
    """General Admin sets member subscription prices and default duration."""
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(default="Member subscription")
    currency: str = Field(default="NGN")
    monthly_price: float = Field(default=1000.0)
    annual_price: float = Field(default=10000.0)
    custom_min_days: int = Field(default=7)
    instructions: Optional[str] = Field(default=None, sa_column=Column(Text))
    bank_name: Optional[str] = None
    account_name: Optional[str] = None
    account_number: Optional[str] = None
    other_details: Optional[str] = Field(default=None, sa_column=Column(Text))
    is_active: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MemberSubscription(SQLModel, table=True):
    """Member subscription request; General Admin confirms; auto-expires."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    member_id: Optional[int] = Field(default=None, foreign_key="churchmember.id")
    plan: str = Field(default="monthly")  # monthly | annual | custom
    amount: float = Field(default=0.0)
    currency: str = Field(default="NGN")
    duration_days: int = Field(default=30)
    status: str = Field(default="pending")  # pending | active | expired | rejected
    payment_reference: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    confirmed_by: Optional[int] = None
    note: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)
