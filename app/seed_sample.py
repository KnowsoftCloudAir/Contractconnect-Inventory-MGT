"""Sample: Knowsoft Church — hierarchy, sub-admins, members, weekly stats."""
from datetime import date, timedelta
from sqlmodel import Session, select
from app.models import MusicLink, DistrictMessage, User, UserRole, ChurchUnit, ChurchLevel, ChurchMember, WeeklyStat, SpecialProgram
from app.auth import get_password_hash

FIRST = ["David", "Grace", "Samuel", "Ruth", "Michael", "Esther", "Daniel", "Hannah",
         "Joseph", "Mary", "Peter", "Sarah", "James", "Joy", "Emmanuel", "Faith",
         "Caleb", "Blessing", "Isaac", "Deborah", "Timothy", "Peace", "Paul", "Hope"]
LAST = ["Okonkwo", "Adeyemi", "Bello", "Okoro", "Mensah", "Nwachukwu", "Ibrahim",
        "Okafor", "Eze", "Chukwu", "Abdullahi", "Ogunleye", "Adebayo", "Nwosu"]

SAMPLE_PASSWORD = "Church@12345"
DATA_PASSWORD = "Data@12345"


def _ensure_admin(session: Session, email: str, name: str, church_id: int,
                  role=UserRole.church_admin, password: str = SAMPLE_PASSWORD, stats: bool = False):
    """Create or reset sample sub-admin so login always works."""
    u = session.exec(select(User).where(User.email == email)).first()
    if u:
        u.hashed_password = get_password_hash(password)
        u.full_name = name
        u.role = role
        u.church_id = church_id
        u.is_active = True
        u.can_enter_stats = stats
        u.can_create_churches = True
        u.can_approve_members = True
        session.add(u)
    else:
        session.add(User(
            email=email,
            hashed_password=get_password_hash(password),
            full_name=name,
            role=role,
            church_id=church_id,
            is_active=True,
            can_enter_stats=stats,
            can_create_churches=True,
            can_approve_members=True,
        ))
    session.commit()



def _ensure_district_sample_data(session: Session, district, global_c, country, state, group):
    """Ensure Allen district has members, 12 weeks of stats, and a sample program."""
    import itertools
    if not district:
        return
    gcid = global_c.id if global_c else None
    ccid = country.id if country else None
    scid = state.id if state else None
    grid = group.id if group else None

    existing_emails = {
        m.email for m in session.exec(
            select(ChurchMember).where(ChurchMember.church_id == district.id)
        ).all() if m.email
    }
    statuses = [
        ("member", None, None), ("member", None, None), ("member", None, None),
        ("worker", "usher", None), ("worker", "choir", None), ("worker", "prayer", None),
        ("worker", "evangelist", None), ("worker", "media", None),
        ("leader", None, "coordinator"), ("leader", None, "women_leader"),
        ("leader", None, "children_leader"), ("leader", None, "bible_study_teacher"),
        ("pastor", None, "group_pastor"),
    ]
    sexes = ["brother", "sister"]
    ages = ["child", "youth", "campus", "adult"]
    conf = ["saved", "saved", "saved", "restored", "backslidden"]
    added = 0
    target = 80
    mcount = len(session.exec(select(ChurchMember).where(ChurchMember.church_id == district.id)).all())
    if mcount < target:
        for i, (fn, ln) in enumerate(itertools.product(FIRST, LAST)):
            if mcount + added >= target:
                break
            email = f"member{i}@knowsoftchurch.sample"
            if email in existing_emails:
                continue
            st, wt, lt = statuses[i % len(statuses)]
            session.add(ChurchMember(
                church_id=district.id,
                global_church_id=gcid,
                country_church_id=ccid,
                state_church_id=scid,
                group_church_id=grid,
                full_name=f"{fn} {ln}",
                sex=sexes[i % 2],
                age_category=ages[i % 4],
                confession=conf[i % 5],
                member_since=date.today() - timedelta(days=30 * (i % 24)),
                whatsapp=f"+23480{1000000 + i}",
                phone=f"+23480{1000000 + i}",
                email=email,
                address=f"{10 + i} Sample Street, Ikeja, Lagos",
                status=st, worker_type=wt, leader_type=lt,
                approval_status="approved", is_active=True,
            ))
            added += 1
        session.commit()
        print(f"✅ District sample members: added {added} (total target {target})")
    else:
        print(f"ℹ️ District already has {mcount} members")

    scount = len(session.exec(select(WeeklyStat).where(WeeklyStat.church_id == district.id)).all())
    if scount < 12:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        existing_weeks = {
            str(s.week_start) for s in session.exec(
                select(WeeklyStat).where(WeeklyStat.church_id == district.id)
            ).all()
        }
        for w in range(12):
            ws = monday - timedelta(weeks=11 - w)
            if str(ws) in existing_weeks:
                continue
            base = 40 + (w % 5) * 3
            session.add(WeeklyStat(
                church_id=district.id, week_start=ws,
                adult_male=base, adult_female=base + 12,
                children_boys=10 + w % 4, children_girls=12 + w % 3,
                youth_male=8 + w % 5, youth_female=9 + w % 4,
                offering=50000 + w * 2500, tithe=120000 + w * 5000, donation=15000 + w * 1000,
                newcomers=4 + w % 3, converts=2 + w % 2,
                counseling=2 + w % 3, members_in_need=3 + w % 4,
                notes="Knowsoft Church Allen – sample week",
            ))
        session.commit()
        print("✅ District weekly stats ensured (12 weeks)")
    else:
        print(f"ℹ️ District already has {scount} weekly stats")

    prog = session.exec(
        select(SpecialProgram).where(
            SpecialProgram.church_id == district.id,
            SpecialProgram.title == "Thanksgiving & Dedication Service",
        )
    ).first()
    if not prog:
        session.add(SpecialProgram(
            church_id=district.id,
            title="Thanksgiving & Dedication Service",
            description="Special thanksgiving service. All Ikeja Group members invited.",
            program_date=date.today() + timedelta(days=7),
            location="Allen Avenue Auditorium",
            broadcast_to="group",
            is_active=True,
            featured_on_home=False,
        ))
        session.commit()
        print("✅ District sample program created")

    district.approval_status = "approved"
    district.is_active = True
    district.address = district.address or "12 Allen Avenue, Ikeja, Lagos"
    district.resident_pastor = district.resident_pastor or "Pastor Ruth Okoro"
    if getattr(district, "latitude", None) is None:
        district.latitude = 6.6018
    if getattr(district, "longitude", None) is None:
        district.longitude = 3.3515
    district.country_name = district.country_name or "Nigeria"
    district.state_name = district.state_name or "Lagos"
    session.add(district)
    session.commit()



def seed_knowsoft_bible_church(session: Session) -> None:
    """Always ensure full Knowsoft sample hierarchy + district data exist."""
    try:
        def unit_get_or_create(code: str, **kw):
            c = session.exec(select(ChurchUnit).where(ChurchUnit.code == code)).first()
            if c:
                c.approval_status = "approved"
                c.is_active = True
                for k, v in kw.items():
                    if v is not None and hasattr(c, k):
                        setattr(c, k, v)
                session.add(c)
                session.commit()
                session.refresh(c)
                return c
            c = ChurchUnit(code=code, approval_status="approved", is_active=True, **kw)
            session.add(c)
            session.commit()
            session.refresh(c)
            return c

        global_c = unit_get_or_create(
            "KC-GLOBAL",
            name="Knowsoft Church",
            level=ChurchLevel.global_church,
            global_code="KC-GLOBAL",
            doctrine="Scripture-based faith, salvation in Christ, discipleship and mission.",
            activity_days="Sunday, Wednesday, Friday",
            owner_name="Apostle David Knowsoft",
            resident_pastor="Apostle David Knowsoft",
            address="Knowsoft Global HQ, Abuja",
            phone="+234-800-100-0001",
            email="global@knowsoftchurch.org",
            country_name="Nigeria",
            tithe_account_name="Knowsoft Church Global",
            tithe_account_number="0123456789",
            tithe_bank_name="First Bank",
            offering_account_name="Knowsoft Church Global Offering",
            offering_account_number="0123456790",
            offering_bank_name="First Bank",
            weekly_activities_note="Sunday 8am & 10am · Midweek Wednesday 6pm · Friday prayer 9pm",
        )
        country = unit_get_or_create(
            "KC-NG",
            name="Knowsoft Church – Nigeria",
            level=ChurchLevel.country,
            parent_id=global_c.id,
            global_code="KC-GLOBAL",
            country_code="KC-NG",
            country_name="Nigeria",
            resident_pastor="Rev. Samuel Okonkwo",
            email="nigeria@knowsoftchurch.org",
            address="National Office, Abuja, Nigeria",
        )
        state = unit_get_or_create(
            "KC-NG-LAG",
            name="Knowsoft Church – Lagos State",
            level=ChurchLevel.state,
            parent_id=country.id,
            global_code="KC-GLOBAL",
            country_code="KC-NG",
            state_code="KC-NG-LAG",
            country_name="Nigeria",
            state_name="Lagos",
            resident_pastor="Pastor Grace Adeyemi",
            email="lagos@knowsoftchurch.org",
            address="Lagos State Office, Ikeja",
            latitude=6.5244,
            longitude=3.3792,
        )
        group = unit_get_or_create(
            "KC-NG-LAG-IKE",
            name="Knowsoft Church – Ikeja Group",
            level=ChurchLevel.group,
            parent_id=state.id,
            global_code="KC-GLOBAL",
            country_code="KC-NG",
            state_code="KC-NG-LAG",
            group_code="KC-NG-LAG-IKE",
            country_name="Nigeria",
            state_name="Lagos",
            resident_pastor="Pastor Michael Bello",
            email="ikeja@knowsoftchurch.org",
        )
        district = unit_get_or_create(
            "KC-NG-LAG-IKE-ALLEN",
            name="Knowsoft Church – Allen Avenue District",
            level=ChurchLevel.district,
            parent_id=group.id,
            global_code="KC-GLOBAL",
            country_code="KC-NG",
            state_code="KC-NG-LAG",
            group_code="KC-NG-LAG-IKE",
            district_code="KC-NG-LAG-IKE-ALLEN",
            country_name="Nigeria",
            state_name="Lagos",
            resident_pastor="Pastor Ruth Okoro",
            email="allen@knowsoftchurch.org",
            address="Allen Avenue, Ikeja, Lagos",
            phone="+234-800-100-0005",
            pastor_phone="+234-800-100-0005",
            pastor_email="allen@knowsoftchurch.org",
            tithe_account_name="Knowsoft Allen District",
            tithe_account_number="3012345678",
            tithe_bank_name="GTBank",
            offering_account_name="Knowsoft Allen Offering",
            offering_account_number="3012345679",
            offering_bank_name="GTBank",
            weekly_activities_note="Sunday celebration 8am & 10am · Bible study Wed 6pm · Prayer Fri 9pm",
            latitude=6.6018,
            longitude=3.3515,
        )

        _ensure_district_sample_data(session, district, global_c, country, state, group)
        print("✅ Knowsoft sample churches ready: Global → Nigeria → Lagos → Ikeja → Allen District")
    except Exception as e:
        print(f"⚠️ Sample seed error: {e}")
        import traceback
        traceback.print_exc()


def ensure_all_sample_data(session: Session) -> None:
    """Single entry: hierarchy, stats, music, sample member, password resets."""
    seed_knowsoft_bible_church(session)
    # Sub-admins
    samples = [
        ("global@knowsoftchurch.org", "Apostle David Knowsoft", "KC-GLOBAL", SAMPLE_PASSWORD, UserRole.church_admin, False),
        ("nigeria@knowsoftchurch.org", "Rev. Samuel Okonkwo", "KC-NG", SAMPLE_PASSWORD, UserRole.church_admin, False),
        ("lagos@knowsoftchurch.org", "Pastor Grace Adeyemi", "KC-NG-LAG", SAMPLE_PASSWORD, UserRole.church_admin, False),
        ("ikeja@knowsoftchurch.org", "Pastor Michael Bello", "KC-NG-LAG-IKE", SAMPLE_PASSWORD, UserRole.church_admin, False),
        ("allen@knowsoftchurch.org", "Pastor Ruth Okoro", "KC-NG-LAG-IKE-ALLEN", SAMPLE_PASSWORD, UserRole.church_admin, False),
        ("data@allen.knowsoftchurch.org", "Bro. James Data Officer", "KC-NG-LAG-IKE-ALLEN", DATA_PASSWORD, UserRole.data_officer, True),
    ]
    for email, name, code, pwd, role, stats in samples:
        unit = session.exec(select(ChurchUnit).where(ChurchUnit.code == code)).first()
        if unit:
            unit.approval_status = "approved"
            unit.is_active = True
            session.add(unit)
            session.commit()
            _ensure_admin(session, email, name, unit.id, role, pwd, stats)
    try:
        seed_music_links(session)
    except Exception as e:
        print(f"⚠️ Music seed: {e}")
    try:
        seed_sample_member(session)
    except Exception as e:
        print(f"⚠️ Sample member: {e}")
    print("✅ Sample logins:")
    print("   General Admin:  admin@knowsoft.com / Admin@12345")
    print("   Global:         global@knowsoftchurch.org / Church@12345")
    print("   Country (NG):   nigeria@knowsoftchurch.org / Church@12345")
    print("   State (Lagos):  lagos@knowsoftchurch.org / Church@12345")
    print("   Group (Ikeja):  ikeja@knowsoftchurch.org / Church@12345")
    print("   District:       allen@knowsoftchurch.org / Church@12345")
    print("   Data officer:   data@allen.knowsoftchurch.org / Data@12345")
    print("   Sample member:  member@knowsoftchurch.org / Member@12345")

def seed_music_links(session: Session) -> None:
    """Default YouTube worship list; General Admin can edit later."""
    from app.models import MusicLink
    count = len(list(session.exec(select(MusicLink)).all()))
    if count > 0:
        print(f"ℹ️ Music links already present ({count})")
        return
    defaults = [
        ("10,000 Reasons (Bless the Lord) – Matt Redman", "DXDGE_lRI0E", 0),
        ("Way Maker – Sinach", "hH-i5QUXcp0", 1),
        ("Reckless Love – Cory Asbury", "Sc6SSTrIEQU", 2),
        ("The Blessing – Kari Jobe", "Zp6aygmvzM4", 3),
        ("Gratitude – Brandon Lake", "yNkQx0AdhTE", 4),
        ("Holy Forever – Chris Tomlin", "9R5WFa0T_y0", 5),
        ("Firm Foundation (He Won't) – Cody Carnes", "eh9bA4S1zxs", 6),
        ("Yet Not I But Through Christ In Me", "hwc2d1Xt8IA", 7),
        ("Great Are You Lord", "ZZsQLb1Nm6A", 8),
        ("Raise A Hallelujah – Bethel", "kwl2n6z4xT4", 9),
        ("Amazing Grace (My Chains Are Gone)", "J3iB5BFs1OQ", 10),
        ("How Great Is Our God – Chris Tomlin", "2fngvQSInoq", 11),
        ("Oceans – Hillsong United", "00-6OyXVA0M", 12),
        ("What A Beautiful Name – Hillsong", "zmqhqf01A-g", 13),
        ("Build My Life – Pat Barrett", "Jbe7OruLk8I", 14),
        ("In Christ Alone", "y8ZonPhl0-A", 15),
        ("Goodness of God", "4_X-nLQDn4M", 16),
        ("King of Kings – Hillsong", "GZX6_Bf1xwQ", 17),
        ("No Longer Slaves – Bethel", "d-diB65scQU", 18),
        ("Goodness of God – Bethel Music (live)", "n0F86oCaR14", 19),
        ("Way Maker – Leeland live", "EXMTxftbIkw", 20),
        ("Reckless Love – official audio/visual", "6xlpLWn8Svg", 21),
        ("The Blessing – Elevation & Kari Jobe live", "Zp6aygmvzM4", 22),
        ("Build My Life – Housefires", "ZLyiirWM1j8", 23),
        ("What A Beautiful Name – Hillsong live", "r5LkSsFhG5I", 24),
        ("Oceans – Hillsong UNITED live", "dy9nwe9_xzw", 25),
        ("10,000 Reasons – Matt Redman live", "XswkNjAZPj4", 26),
        ("How Great Is Our God – live", "KBDCj8DhF8M", 27),
        ("Amazing Grace My Chains – live", "J3iB5BFs1OQ", 28),
        ("Holy Forever – live worship", "N-6s_p_zJ1c", 29),
    ]
    for title, yid, order in defaults:
        session.add(MusicLink(title=title, youtube_id=yid, is_active=True, sort_order=order))
    session.commit()
    print("✅ Default music links seeded")


def seed_sample_member(session: Session) -> None:
    """Approved sample member for testing: member@knowsoftchurch.org / Member@12345"""
    from app.models import ChurchMember, User, UserRole, ChurchUnit, ChurchLevel
    from app.auth import get_password_hash
    email = "member@knowsoftchurch.org"
    password = "Member@12345"
    district = session.exec(
        select(ChurchUnit).where(ChurchUnit.code == "KC-NG-LAG-IKE-ALLEN")
    ).first()
    if not district:
        district = session.exec(
            select(ChurchUnit).where(ChurchUnit.level == ChurchLevel.district)
        ).first()
    if not district:
        print("⚠️ No district for sample member")
        return
    member = session.exec(select(ChurchMember).where(ChurchMember.email == email)).first()
    if not member:
        member = ChurchMember(
            church_id=district.id,
            full_name="Sister Faith Sample",
            email=email,
            sex="sister",
            age_group="adult",
            confession="saved",
            status="member",
            approval_status="approved",
            phone="+2348000000001",
            whatsapp="+2348000000001",
        )
        session.add(member)
        session.commit()
        session.refresh(member)
    else:
        member.approval_status = "approved"
        member.church_id = district.id
        session.add(member)
        session.commit()
    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        user = User(
            email=email,
            full_name="Sister Faith Sample",
            hashed_password=get_password_hash(password),
            role=UserRole.member,
            is_active=True,
            is_sample_account=True,
            sample_started_at=None,
            church_id=district.id,
            member_id=member.id,
        )
        session.add(user)
    else:
        user.hashed_password = get_password_hash(password)
        user.is_active = True
        user.role = UserRole.member
        user.is_sample_account = True
        user.church_id = district.id
        user.member_id = member.id
        session.add(user)
    session.commit()
    print(f"✅ Sample member ready: {email} / {password}")
