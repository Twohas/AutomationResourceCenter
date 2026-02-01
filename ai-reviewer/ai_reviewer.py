import os
import json
import requests
import google.generativeai as genai
from github import Github, Auth

# 1. 설정값 가져오기
gemini_api_key = os.getenv("GEMINI_API_KEY")
github_token = os.getenv("GITHUB_TOKEN")
pr_number_str = os.getenv("PR_NUMBER")
repo_name = os.getenv("GITHUB_REPOSITORY")
webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

gemini_model = "gemini-2.5-flash-lite"

# 유효성 검사
if not gemini_api_key:
    print("❌ Error: GEMINI_API_KEY is missing")
    exit(1)
if not pr_number_str:
    print("❌ Error: PR_NUMBER is missing")
    exit(1)

# 2. Gemini 설정 (Gemini 1.5 Flash 모델 사용)
genai.configure(api_key=gemini_api_key)

model_json = genai.GenerativeModel(gemini_model, generation_config={"response_mime_type": "application/json"})
model_text = genai.GenerativeModel(gemini_model)

auth = Auth.Token(github_token)
g = Github(auth=auth)
repo = g.get_repo(repo_name)
pr = repo.get_pull(int(pr_number_str))
last_commit = list(pr.get_commits())[-1]

print(f"🚀 리뷰 시작 (Model: {gemini_model})")

# 3. 변경된 파일별로 리뷰 데이터 수집
review_comments = []
all_diffs_context = "" # 요약을 위해 전체 코드를 모을 변수
issue_count = 0

# ------------------------------------------------------------------
# 단계 1: 파일별 루프 (인라인 리뷰 수집 + 전체 Diff 모으기)
# ------------------------------------------------------------------
for file in pr.get_files():
    if file.status == "removed" or file.patch is None:
        continue
    
    print(f"🔍 Analyzing: {file.filename}")

    # 1-1. 전체 Diff 수집 (너무 길면 자름 - 토큰 제한 방지)
    if len(all_diffs_context) < 30000:
        all_diffs_context += f"\n\n--- File: {file.filename} ---\n{file.patch}"

    # 1-2. 인라인 리뷰 프롬프트 (JSON 요청)
    prompt = f"""
    너는 구글, 애플 출신의 시니어 개발자야. 아래 제공되는 Git Diff 코드를 분석해서 코드 리뷰를 해줘.
    
    **파일명:** {file.filename}
    
    **목표:**
    1. 버그, 성능 이슈, 스타일 가이드 위반, 안티 패턴을 찾아내.
    2. 중요하지 않은 변경사항은 무시해. (리뷰 노이즈 최소화)

    **출력 형식 (JSON List):**
    반드시 아래 JSON 구조의 리스트로만 응답해. 마크다운 코드블럭을 쓰지 말고 순수 JSON만 출력해.
    
    [
      {{
        "line": <int: 이슈가 발견된 변경 후 파일의 라인 번호>,
        "category": "<string: '이슈' | '제안'>",
        "severity": "<string: 'Critical' | ''Major' | 'Minor'>",
        "message": "<string: 리뷰 내용 (한국어)>"
      }}
    ]

    **코멘트 스타일 가이드:**
    - CodeRabbit 스타일을 따라해.
    - 친절하지만 명확하고 간결하게 설명해.

    --- Git Diff ---
    {file.patch}
    """

    try:
        response = model_json.generate_content(prompt)
        # JSON 파싱 및 예외처리
        text = response.text.strip()
        if text.startswith("```json"): text = text[7:-3]
        elif text.startswith("```"): text = text[3:-3]
            
        comments_data = json.loads(text)

        for item in comments_data:
            issue_count += 1

            if item['category'] == '이슈':   icon = "⚠️"
            else:                           icon = "💡"

            if item['severity'] == 'Critical':  severity_icon = "🔥" # Critical은 불꽃 아이콘
            elif item['severity'] == 'Major':   severity_icon = "🔴"
            else:                               severity_icon = "🟡"

            body = f"### {icon} {item['category']} | {severity_icon} {item['severity']}\n\n{item['message']}"

            review_comments.append({
                "path": file.filename,
                "line": int(item['line']),
                "body": body
            })

    except Exception as e:
        print(f"⚠️ {file.filename} 처리 중 에러: {e}")
        continue

# ------------------------------------------------------------------
# 단계 2: 인라인 리뷰 등록 (CodeRabbit 스타일)
# ------------------------------------------------------------------
if review_comments:
    try:
        print(f"📨 {len(review_comments)}개의 인라인 코멘트 등록 중...")
        pr.create_review(
            commit=last_commit,
            body="## 🤖 Gemini AI Code Review\n리뷰가 도착했습니다! 코드를 확인해주세요.",
            event="COMMENT",
            comments=review_comments
        )
        print("✅ 인라인 리뷰 등록 완료!")
    except Exception as e:
        print(f"❌ 리뷰 등록 실패: {e}")
        
# ------------------------------------------------------------------
# 단계 3: PR 본문(Description) 업데이트 (요약 및 주요 변경점)
# ------------------------------------------------------------------
print("📝 전체 변경 사항 요약 중...")

summary_prompt = f"""
너는 테크 리드야. 아래 제공된 전체 코드 변경 사항(Diff 모음)을 보고 PR 설명을 작성해 줘.
반드시 **한국어**로 작성해.

**요청 사항:**
1. **📌 3줄 요약:** 전체 변경 내용을 3줄 이내로 핵심만 요약해.
2. **🔍 주요 변경점:** 변경된 내용을 기능 단위로 글머리 기호(Bullet points)로 정리해.
3. 기술적인 내용은 정확하게, 어조는 정중하게.

--- Diff Context (Truncated) ---
{all_diffs_context[:30000]}
"""

try:
    summary_response = model_text.generate_content(summary_prompt)
    summary_text = summary_response.text.strip()

    # AI 영역 표시 마커 (이 주석 사이의 내용만 AI가 업데이트함)
    marker_start = ""
    marker_end = ""

    current_body = pr.body if pr.body else ""
    
    # 마커로 감싼 새로운 AI 컨텐츠 생성
    new_ai_section = f"{marker_start}\n## 🤖 AI 요약\n\n{summary_text}\n{marker_end}"

    if marker_start in current_body and marker_end in current_body:
        # 이미 AI 요약이 있다면, 해당 부분만 교체 (정규식 없이 단순 문자열 처리)
        start_idx = current_body.find(marker_start)
        end_idx = current_body.find(marker_end) + len(marker_end)
        
        # 기존 앞부분 + 새 AI 요약 + 기존 뒷부분
        final_body = current_body[:start_idx] + new_ai_section + current_body[end_idx:]
    else:
        # AI 요약이 없다면, 본문 맨 위에 추가 (또는 맨 아래)
        # 보통 요약은 맨 위가 좋으므로 맨 위에 배치
        final_body = f"{new_ai_section}\n\n{current_body}"

    # PR 업데이트
    pr.edit(body=final_body)
    print("✅ PR 본문(Description) 업데이트 완료!")

except Exception as e:
    print(f"❌ PR 요약 생성/업데이트 실패: {e}")

# ------------------------------------------------------------------
# 단계 4: 디스코드 알림 전송
# ------------------------------------------------------------------
if webhook_url:
    print("🔔 디스코드 알림 전송 중...")
    try:
        # 메시지 내용 구성 (Embed 사용)
        payload = {
            "username": "Gemini Code Reviewer",
            "avatar_url": "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png",
            "embeds": [
                {
                    "title": f"🤖 AI 리뷰 완료: #{pr_number_str} {pr.title}",
                    "url": pr.html_url,
                    "color": 5814783, # 보라색 계열
                    "fields": [
                        {
                            "name": "📊 분석 결과",
                            "value": f"발견된 코멘트: **{issue_count}개**",
                            "inline": True
                        }
                    ],
                    "footer": {
                        "text": f"Repo: {repo_name} • Requested by {pr.user.login}"
                    }
                }
            ]
        }
        
        requests.post(webhook_url, json=payload)
        print("✅ 디스코드 알림 전송 완료!")
        
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")
else:
    print("ℹ️ DISCORD_WEBHOOK_URL이 설정되지 않아 알림을 건너뜁니다.")
