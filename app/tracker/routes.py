from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, Response, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import db
from app.leaderboard.cache import invalidate_leaderboard_cache
from app.profile.card_service import warm_public_card_cache
from app.utils import (
    compute_in_sheet_platform_counts,
    json_error,
    json_success,
    platform_from_question_url,
    question_editorial_links,
    update_computed_stats,
    utc_now,
)
from calendar_export import build_study_plan_ics
from notes_export import build_all_notes_markdown, build_topic_notes_markdown, topic_notes_filename
from progress_export import build_progress_csv
from progress_import import parse_csv_backup, parse_json_backup, process_dry_run


tracker_bp = Blueprint("tracker", __name__)

DIFFICULTY_FILTERS = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
}


def normalize_difficulty_filter(raw_filter):
    value = (raw_filter or "all").strip().lower()
    if value == "all":
        return "all"
    return DIFFICULTY_FILTERS.get(value, "all")
INDEX_QUESTION_PROJECTION = {"topic": 1}
TOPIC_PAGE_QUESTION_PROJECTION = {
    "problem": 1,
    "difficulty": 1,
    "url": 1,
    "url2": 1,
    "editorial_links": 1,
}
TOPIC_NOTES_EXPORT_PROJECTION = {"problem": 1}
QUESTION_STATUS_PROJECTION = {"problem": 1, "url": 1}
BOOKMARKS_QUESTION_PROJECTION = {
    "topic": 1,
    "problem": 1,
    "url": 1,
    "url2": 1,
    "editorial_links": 1,
}
CSV_EXPORT_QUESTION_PROJECTION = {
    "topic": 1,
    "problem": 1,
    "difficulty": 1,
    "url": 1,
    "url2": 1,
}
ALL_NOTES_QUESTION_PROJECTION = {"problem": 1, "topic": 1}

@tracker_bp.route("/")
def index():
    pre = current_app.config.get("_PRECOMPUTED")
    if pre:
        topics = pre["topics"]
        total_questions = pre["total_questions"]
        topic_question_count = pre["topic_question_count"]
    else:
        topics = list(db.topic.find().sort("position", 1))
        total_questions = db.question.count_documents({})
        all_questions = list(db.question.find({}, INDEX_QUESTION_PROJECTION))
        topic_question_count = {}
        for question in all_questions:
            topic_id = str(question["topic"])
            topic_question_count.setdefault(topic_id, []).append(str(question["_id"]))

    if current_user.is_authenticated:
        progress = current_user.progress if isinstance(current_user.progress, dict) else {}
        done_questions = sum(1 for progress_item in progress.values() if progress_item.get("done"))
    else:
        progress = {}
        done_questions = 0

    topic_progress = {}
    for topic in topics:
        topic_id = str(topic["_id"])
        topic_question_ids = topic_question_count.get(topic_id, [])
        if current_user.is_authenticated:
            topic_done = sum(1 for question_id in topic_question_ids if progress.get(question_id, {}).get("done"))
        else:
            topic_done = 0
        topic_progress[topic_id] = {"done": topic_done, "total": len(topic_question_ids)}

    return render_template(
        "index.html",
        topics=topics,
        total_questions=total_questions,
        done_questions=done_questions,
        topic_progress=topic_progress,
    )


@tracker_bp.route("/topic/<topic_id>")
def topic(topic_id):
    try:
        topic_id_obj = ObjectId(topic_id)
    except InvalidId:
        return "Topic not found", 404
    topic_doc = db.topic.find_one({"_id": topic_id_obj})
    if not topic_doc:
        return "Topic not found", 404

    questions = list(db.question.find({"topic": topic_doc["_id"]}, TOPIC_PAGE_QUESTION_PROJECTION))
    for question in questions:
        question["editorial_links"] = question_editorial_links(question)
        
    progress_dict = current_user.progress if (current_user.is_authenticated and isinstance(current_user.progress, dict)) else {}
    
    # Calculate counts based on the unfiltered list of questions
    total_count = len(questions)
    easy_count = sum(1 for q in questions if q.get('difficulty', 'Medium') == 'Easy')
    medium_count = sum(1 for q in questions if q.get('difficulty', 'Medium') == 'Medium')
    hard_count = sum(1 for q in questions if q.get('difficulty', 'Medium') == 'Hard')
    done_count = sum(1 for q in questions if progress_dict.get(str(q["_id"]), {}).get("done"))
    skipped_count = sum(1 for q in questions if progress_dict.get(str(q["_id"]), {}).get("skipped"))
    todo_count = total_count - done_count - skipped_count
    
    # Get difficulty filter from query parameter
    difficulty_filter = normalize_difficulty_filter(request.args.get('difficulty', 'all'))
    status_filter = request.args.get('status', 'all')
    
    if difficulty_filter != 'all':
        questions = [q for q in questions if q.get('difficulty', 'Medium') == difficulty_filter]

    if status_filter == 'done':
        questions = [q for q in questions if progress_dict.get(str(q["_id"]), {}).get("done")]
    elif status_filter == 'skipped':
        questions = [q for q in questions if progress_dict.get(str(q["_id"]), {}).get("skipped")]
    elif status_filter == 'todo':
        questions = [
            q for q in questions
            if not progress_dict.get(str(q["_id"]), {}).get("done")
            and not progress_dict.get(str(q["_id"]), {}).get("skipped")
        ]

    active_filters = []
    if difficulty_filter != 'all':
        active_filters.append(f"{difficulty_filter} difficulty")
    if status_filter != 'all':
        active_filters.append(f"{status_filter.capitalize()} status")
    
    return render_template(
        "topic.html", 
        topic=topic_doc, 
        questions=questions, 
        progress_dict=progress_dict,
        difficulty_filter=difficulty_filter,
        status_filter=status_filter,
        active_filters=", ".join(active_filters),
        total_count=total_count,
        easy_count=easy_count,
        medium_count=medium_count,
        hard_count=hard_count,
        done_count=done_count,
        skipped_count=skipped_count,
        todo_count=todo_count,
    )


@tracker_bp.route("/topic/<topic_id>/export-notes")
@login_required
def export_topic_notes(topic_id):
    try:
        topic_id_obj = ObjectId(topic_id)
    except InvalidId:
        return "Topic not found", 404
    topic_doc = db.topic.find_one({"_id": topic_id_obj})
    if not topic_doc:
        return "Topic not found", 404

    questions = list(db.question.find({"topic": topic_doc["_id"]}, TOPIC_NOTES_EXPORT_PROJECTION))
    markdown = build_topic_notes_markdown(topic_doc["name"], questions, current_user.progress)
    response = Response(markdown, mimetype="text/markdown")
    response.headers["Content-Disposition"] = f'attachment; filename={topic_notes_filename(topic_doc["name"])}'
    return response


@tracker_bp.route('/update_question/<string:question_id>', methods=['POST'])
@login_required
def update_question(question_id):
    # Strict Parsing: Catch malformed JSON safely before it turns into NoneType
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form if request.form else None
    except Exception:
        data = None

    # Explicit check for missing or invalid data body matching test suites exactly
    if data is None or not isinstance(data, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object"}), 400

    # Validate boolean parameters
    for flag in ['done', 'skipped', 'bookmark']:
        if flag in data and not isinstance(data[flag], bool):
            return jsonify({"success": False, "error": f"{flag} must be a boolean"}), 400

    user_id = current_user.id
    user_doc = db.user.find_one({"_id": user_id})
    
    progress = user_doc.get("progress", {}) if user_doc else {}
    if not isinstance(progress, dict):
        progress = {}

    q_str = str(question_id)
    if q_str not in progress:
        progress[q_str] = {"done": False, "skipped": False, "bookmark": False, "notes": "", "confidence": ""}

    # 🔄 MUTUAL EXCLUSION LOGIC (Critical Bug Fix)
    if 'done' in data:
        progress[q_str]['done'] = data['done']
        if data['done']:
            progress[q_str]['skipped'] = False  # If done, it can't be skipped
            
    if 'skipped' in data:
        progress[q_str]['skipped'] = data['skipped']
        if data['skipped']:
            progress[q_str]['done'] = False  # If skipped, it can't be done

    # Update other optional attributes safely
    if 'bookmark' in data:
        progress[q_str]['bookmark'] = data['bookmark']
    if 'notes' in data:
        progress[q_str]['notes'] = data['notes']
    if 'confidence' in data:
        confidence_level = str(data['confidence']).strip().lower()
        progress[q_str]['confidence'] = confidence_level if confidence_level in ['low', 'medium', 'high'] else ''

    # Persist the clean legacy dictionary wrapper structure back to Mongo
    db.user.update_one({"_id": user_id}, {"$set": {"progress": progress}})

    return jsonify({
        "success": True,
        "status": "success",
        "message": "Question updated successfully",
        "confidence": progress[q_str].get("confidence", "")
    }), 200


@tracker_bp.route("/bookmarks")
@login_required
def bookmarks():
    progress = current_user.progress
    bookmarked_question_ids = [question_id for question_id, progress_item in progress.items() if progress_item.get("bookmark")]

    object_ids = []
    for question_id in bookmarked_question_ids:
        try:
            object_ids.append(ObjectId(question_id))
        except InvalidId:
            pass
    questions = list(db.question.find({"_id": {"$in": object_ids}}, BOOKMARKS_QUESTION_PROJECTION))

    topic_ids = list(set(question["topic"] for question in questions))
    topic_docs = {topic["_id"]: topic["name"] for topic in db.topic.find({"_id": {"$in": topic_ids}})}
    for question in questions:
        question["topic_name"] = topic_docs.get(question["topic"], "Unknown")

    return render_template("bookmarks.html", questions=questions, progress_dict=progress)


@tracker_bp.route("/export/csv")
@login_required
def export_csv():
    pre = current_app.config.get("_PRECOMPUTED")
    if pre:
        questions = pre["all_questions"]
        topic_lookup = {tid: t["name"] for tid, t in pre["topic_lookup"].items()}
    else:
        questions = list(db.question.find({}, CSV_EXPORT_QUESTION_PROJECTION))
        topic_ids = list({q.get('topic') for q in questions if q.get('topic')})
        topic_lookup = {
            topic['_id']: topic.get('name', 'Unknown')
            for topic in db.topic.find({'_id': {'$in': topic_ids}}, {'name': 1})
        }
    csv_content = build_progress_csv(questions, topic_lookup, current_user.progress)
    response = Response(csv_content, mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=progress.csv'
    return response


@tracker_bp.route("/export/study-plan.ics")
@login_required
def export_study_plan_ics():
    pre = current_app.config.get("_PRECOMPUTED")
    if pre:
        topics = pre["topics"]
        questions_by_topic = {}
        for q in pre["all_questions"]:
            questions_by_topic.setdefault(q["topic"], []).append(q)
        for topic in topics:
            topic["questions"] = questions_by_topic.get(topic["_id"], [])
    else:
        topics = list(db.topic.find({}, {"name": 1, "position": 1}).sort("position", 1))
        topic_ids = [topic["_id"] for topic in topics]
        questions = list(db.question.find({"topic": {"$in": topic_ids}}, {"topic": 1}))
        questions_by_topic = {}
        for question in questions:
            questions_by_topic.setdefault(question["topic"], []).append(question)
        for topic in topics:
            topic["questions"] = questions_by_topic.get(topic["_id"], [])

    calendar_text = build_study_plan_ics(topics, current_user.progress)
    response = Response(calendar_text, mimetype="text/calendar")
    response.headers["Content-Disposition"] = "attachment; filename=study-plan.ics"
    return response


@tracker_bp.route("/export/all-notes")
@login_required
def export_all_notes():
    """Download all non-empty notes grouped by topic as a single Markdown file."""
    pre = current_app.config.get("_PRECOMPUTED")
    if pre:
        topics = pre["topics"]
        questions_by_topic = {}
        for question in pre["all_questions"]:
            questions_by_topic.setdefault(question["topic"], []).append(question)
    else:
        topics = list(db.topic.find().sort("position", 1))
        all_questions = list(db.question.find({}, ALL_NOTES_QUESTION_PROJECTION))
        questions_by_topic = {}
        for question in all_questions:
            topic_id = str(question["topic"])
            questions_by_topic.setdefault(topic_id, []).append(question)

    markdown = build_all_notes_markdown(
        topics, questions_by_topic, current_user.progress,
    )
    response = Response(markdown, mimetype="text/markdown")
    response.headers["Content-Disposition"] = 'attachment; filename=all_notes.md'
    return response


@tracker_bp.route("/export/json")
@login_required
def export_json():
    questions = list(db.question.find({}, CSV_EXPORT_QUESTION_PROJECTION))
    topic_ids = list({q.get('topic') for q in questions if q.get('topic')})
    topic_lookup = {
        topic['_id']: topic.get('name', 'Unknown')
        for topic in db.topic.find({'_id': {'$in': topic_ids}}, {'name': 1})
    }

    progress = current_user.progress
    exported_progress = []

    for question in questions:
        question_id = str(question.get('_id'))
        item_progress = progress.get(question_id, {}) or {}
        if item_progress.get('done') or item_progress.get('bookmark') or item_progress.get('skipped') or item_progress.get('notes'):
            topic_name = topic_lookup.get(question.get('topic'), 'Unknown')
            exported_progress.append({
                "topic": topic_name,
                "problem": question.get('problem', ''),
                "done": bool(item_progress.get('done', False)),
                "bookmark": bool(item_progress.get('bookmark', False)),
                "skipped": bool(item_progress.get('skipped', False)),
                "notes": item_progress.get('notes', ''),
                "url": question.get('url', ''),
                "url2": question.get('url2', ''),
            })

    backup_data = {
        "version": "1.0",
        "exported_at": utc_now().isoformat(),
        "progress": exported_progress
    }

    import json
    response = Response(json.dumps(backup_data, indent=2), mimetype='application/json')
    response.headers['Content-Disposition'] = 'attachment; filename=progress_backup.json'
    return response


@tracker_bp.route("/progress/import/preview", methods=["POST"])
@login_required
def import_preview():
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files['file']
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No file selected"}), 400

    filename = file.filename.lower()
    content = file.read()
    try:
        content_str = content.decode('utf-8-sig')
    except Exception:
        return jsonify({"success": False, "error": "Unable to decode file. Please upload a UTF-8 encoded text file."}), 400

    if filename.endswith('.csv'):
        parsed_items, err = parse_csv_backup(content_str)
    elif filename.endswith('.json'):
        parsed_items, err = parse_json_backup(content_str)
    else:
        return jsonify({"success": False, "error": "Unsupported file format. Please upload a .csv or .json file."}), 400

    if err:
        return jsonify({"success": False, "error": err}), 400

    questions = list(db.question.find({}, CSV_EXPORT_QUESTION_PROJECTION))
    summary, changes, conflicts, _ = process_dry_run(parsed_items, questions, current_user.progress)

    return jsonify({
        "success": True,
        "summary": summary,
        "changes": changes[:50],
        "conflicts": conflicts
    })


@tracker_bp.route("/progress/import/commit", methods=["POST"])
@login_required
def import_commit():
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    file = request.files['file']
    if not file or not file.filename:
        return jsonify({"success": False, "error": "No file selected"}), 400

    mode = request.form.get("mode", "merge")
    if mode not in ("merge", "replace"):
        return jsonify({"success": False, "error": "Invalid import mode"}), 400

    filename = file.filename.lower()
    content = file.read()
    try:
        content_str = content.decode('utf-8-sig')
    except Exception:
        return jsonify({"success": False, "error": "Unable to decode file. Please upload a UTF-8 encoded text file."}), 400

    if filename.endswith('.csv'):
        parsed_items, err = parse_csv_backup(content_str)
    elif filename.endswith('.json'):
        parsed_items, err = parse_json_backup(content_str)
    else:
        return jsonify({"success": False, "error": "Unsupported file format. Please upload a .csv or .json file."}), 400

    if err:
        return jsonify({"success": False, "error": err}), 400

    questions = list(db.question.find({}, CSV_EXPORT_QUESTION_PROJECTION))
    _, _, _, mapped_progress = process_dry_run(parsed_items, questions, current_user.progress)

    user_id = current_user.id
    current_db_progress = current_user.progress

    new_progress = {}
    if mode == "merge":
        new_progress = dict(current_db_progress)
        for q_id, imp_val in mapped_progress.items():
            existing = new_progress.get(q_id, {})
            done = imp_val["done"] or bool(existing.get("done"))
            bookmark = imp_val["bookmark"] or bool(existing.get("bookmark"))
            skipped = imp_val["skipped"] or bool(existing.get("skipped"))
            if done:
                skipped = False

            db_notes = existing.get("notes") or ""
            imp_notes = imp_val["notes"] or ""

            if db_notes and imp_notes and db_notes != imp_notes:
                notes = f"{db_notes}\n[Imported]: {imp_notes}"
            else:
                notes = imp_notes if imp_notes else db_notes

            timestamp = existing.get("timestamp")
            if imp_val["done"] and not existing.get("done"):
                timestamp = utc_now()
            elif not timestamp:
                timestamp = utc_now()

            new_progress[q_id] = {
                "done": done,
                "bookmark": bookmark,
                "skipped": skipped,
                "notes": notes,
                "timestamp": timestamp
            }
    else:
        for q_id, imp_val in mapped_progress.items():
            existing = current_db_progress.get(q_id, {})
            timestamp = existing.get("timestamp")
            if imp_val["done"] and not existing.get("done"):
                timestamp = utc_now()
            elif not timestamp:
                timestamp = utc_now()

            new_progress[q_id] = {
                "done": imp_val["done"],
                "bookmark": imp_val["bookmark"],
                "skipped": imp_val["skipped"] if not imp_val["done"] else False,
                "notes": imp_val["notes"],
                "timestamp": timestamp
            }

    solved_items = {q_id: prog for q_id, prog in new_progress.items() if prog.get("done")}
    in_sheet_counts = compute_in_sheet_platform_counts(solved_items, questions)

    db.user.update_one(
        {"_id": user_id},
        {
            "$set": {
                "progress": new_progress,
                "in_sheet_platform_counts": in_sheet_counts
            }
        }
    )

    current_user.reload()
    invalidate_leaderboard_cache()
    warm_public_card_cache(user_id, db_handle=db)

    return jsonify({"success": True, "message": "Progress imported successfully!"})
