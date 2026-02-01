import os
import json
import requests
from github import Github, Auth
from openai import OpenAI  # google.generativeai 대신 사용

# 1. 설정값 가져오기
# 내 LLM 설정 (OpenAI 호환 API)
llm_api_key = os.getenv("LLM_API_KEY", "EMPTY") # 로컬 모델은 키가 필요 없는 경우가 많음
llm_base_url = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")      # 예: "http://localhost:11434/v1" (Ollama)
llm_model_name = os.getenv("LLM_MODEL", "qwen2.5-coder:7b")    # 예: "llama3", "deepseek-coder", "qwen2.5-coder:7b"

github_token = os.getenv("GITHUB_TOKEN")
pr_number_str = os.getenv("PR_NUMBER")
repo_name = os.getenv("GITHUB_REPOSITORY")
webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

def get_valid_lines(patch):
    """
    Git Patch 텍스트를 파싱하여 코멘트 가능한(변경된) 라인 번호들의 집합(Set)을 반환합니다.
    """
    valid_lines = set()
    current_line_num = 0
    
    for line in patch.split('\n'):
        # 1. 헝크 헤더 파싱 (예: @@ -10,5 +20,7 @@) -> 새 파일의 시작 라인(+20) 추출
        if line.startswith('@@'):
            match = re.search(r'\+(\d+)', line)
            if match:
                current_line_num = int(match.group(1))
            continue # 헤더 줄 자체는 코멘트 대상 아님

        # 2. 변경되지 않은 줄(공백)이나 추가된 줄(+)은 유효한 라인
        if line.startswith(' ') or line.startswith('+'):
            valid_lines.add(current_line_num)
            current_line_num += 1
            
        # 3. 삭제된 줄(-)은 새 파일에 없으므로 라인 번호 증가 없음
        elif line.startswith('-'):
            pass
            
    return valid_lines

# 유효성 검사
if not llm_base_url or not llm_model_name:
    print("❌ Error: LLM_BASE_URL or LLM_MODEL_NAME is missing")
    exit(1)
if not pr_number_str:
    print("❌ Error: PR_NUMBER is missing")
    exit(1)

# 2. OpenAI 클라이언트 설정 (내 LLM 연결)
client = OpenAI(
    api_key=llm_api_key,
    base_url=llm_base_url
)

auth = Auth.Token(github_token)
g = Github(auth=auth)
repo = g.get_repo(repo_name)
pr = repo.get_pull(int(pr_number_str))
last_commit = list(pr.get_commits())[-1]

print(f"🚀 리뷰 시작 (Model: {llm_model_name} at {llm_base_url})")

# 3. 변경된 파일별로 리뷰 데이터 수집
review_comments = []
all_diffs_context = "" 
issue_count = 0

# ------------------------------------------------------------------
# 단계 1: 파일별 루프
# ------------------------------------------------------------------
IGNORED_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.svg', '.json', '.lock', '.pbxproj', '.xib', '.storyboard']
for file in pr.get_files():
    # 1. 삭제된 파일 패스
    if file.status == "removed" or file.patch is None:
        continue

    # 2. 중요하지 않은 확장자 패스
    if any(file.filename.endswith(ext) for ext in IGNORED_EXTENSIONS):
        print(f"🚫 Skipping (Ignored type): {file.filename}")
        continue
    
    print(f"🔍 Analyzing: {file.filename}")

    valid_lines_in_diff = get_valid_lines(file.patch)

    if len(all_diffs_context) < 30000:
        all_diffs_context += f"\n\n--- File: {file.filename} ---\n{file.patch}"

    # 프롬프트 (JSON 형식 강제)
    system_prompt = "You are a code reviewer. You must output only valid JSON."
    user_prompt = f"""
    너는 구글, 애플 출신의 시니어 개발자야. 아래 제공되는 Git Diff 코드를 분석해서 코드 리뷰를 해줘.
    
    **파일명:** {file.filename}
    
    **목표:**
    1. 버그, 성능 이슈, 스타일 가이드 위반, 안티 패턴을 찾아내.
    2. 중요하지 않은 변경사항은 무시해. (리뷰 노이즈 최소화)

    **출력 형식 (JSON List):**
    반드시 아래 JSON 구조의 리스트로만 응답해. 설명이나 마크다운 코드블럭(```json) 없이 순수 JSON 텍스트만 출력해.
    
    [
      {{
        "line": <int: 이슈가 발견된 변경 후 파일의 라인 번호>,
        "category": "<string: '이슈' | '제안'>",
        "severity": "<string: 'Critical' | 'Major' | 'Minor'>",
        "message": "<string: 리뷰 내용 (한국어)>"
      }}
    ]

    --- Git Diff ---
    {file.patch}
    """

    try:
        # 내 LLM 호출
        response = client.chat.completions.create(
            model=llm_model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2, # 정형화된 출력을 위해 낮음
            # response_format={"type": "json_object"} # 모델이 지원하면 주석 해제하여 사용
        )
        
        text = response.choices[0].message.content.strip()

        # JSON 파싱 전처리 (마크다운 제거)
        if text.startswith("```json"): text = text[7:-3]
        elif text.startswith("```"): text = text[3:-3]
            
        comments_data = json.loads(text)

        if isinstance(comments_data, dict):
            comments_data = [comments_data]

        # ✅ [위치 2-2] 기존의 for item loop를 아래 코드로 통째로 교체하세요!
        for item in comments_data:
            # 필수 키 확인
            if 'line' not in item or 'message' not in item:
                continue
            
            # 라인 번호 정수 변환
            try:
                line_num = int(item['line'])
            except ValueError:
                continue # 숫자가 아니면 스킵

            # 🚨 핵심 수정: Diff 범위 밖인지 검사 (422 에러 방지)
            if line_num not in valid_lines_in_diff:
                print(f"🚫 스킵: 라인 {line_num}은 Diff 범위 밖입니다. (AI가 변경되지 않은 줄을 지적함)")
                continue

            # 여기까지 통과하면 유효한 이슈임
            issue_count += 1
            
            # 아이콘 및 심각도 설정
            icon = "⚠️" if item.get('category') == '이슈' else "💡"
            severity = item.get('severity', 'Minor')
            
            if severity == 'Critical': severity_icon = "🔥"
            elif severity == 'Major':  severity_icon = "🔴"
            else:                      severity_icon = "🟡"

            body = f"### {icon} {item.get('category', '리뷰')} | {severity_icon} {severity}\n\n{item['message']}"

            review_comments.append({
                "path": file.filename,
                "line": line_num,
                "body": body
            })

    except json.JSONDecodeError:
        print(f"⚠️ JSON 파싱 실패 ({file.filename}): 모델이 올바른 JSON을 반환하지 않았습니다.")
        # print(text) # 디버깅용
    except Exception as e:
        print(f"⚠️ {file.filename} 처리 중 에러: {e}")
        continue

# ------------------------------------------------------------------
# 단계 2: 인라인 리뷰 등록
# ------------------------------------------------------------------
if review_comments:
    try:
        print(f"📨 {len(review_comments)}개의 인라인 코멘트 등록 중...")
        pr.create_review(
            commit=last_commit,
            body=f"## 🤖 {llm_model_name} Code Review\n리뷰가 도착했습니다! 코드를 확인해주세요.",
            event="COMMENT",
            comments=review_comments
        )
        print("✅ 인라인 리뷰 등록 완료!")
    except Exception as e:
        print(f"❌ 리뷰 등록 실패: {e}")
        
# ------------------------------------------------------------------
# 단계 3: PR 본문 업데이트
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
    summary_response = client.chat.completions.create(
        model=llm_model_name,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": summary_prompt}
        ],
        temperature=0.5
    )
    summary_text = summary_response.choices[0].message.content.strip()

    # AI 영역 표시 마커
    marker_start = ""
    marker_end = ""

    current_body = pr.body if pr.body else ""
    new_ai_section = f"{marker_start}\n## 🤖 AI 요약 ({llm_model_name})\n\n{summary_text}\n{marker_end}"

    if marker_start in current_body and marker_end in current_body:
        start_idx = current_body.find(marker_start)
        end_idx = current_body.find(marker_end) + len(marker_end)
        final_body = current_body[:start_idx] + new_ai_section + current_body[end_idx:]
    else:
        final_body = f"{new_ai_section}\n\n{current_body}"

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
        payload = {
            "username": "AI Code Reviewer",
            "avatar_url": "[https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png](https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png)",
            "embeds": [
                {
                    "title": f"🤖 AI 리뷰 완료: #{pr_number_str} {pr.title}",
                    "url": pr.html_url,
                    "color": 5814783,
                    "fields": [
                        {
                            "name": "📊 분석 결과",
                            "value": f"모델: {llm_model_name}\n발견된 코멘트: **{issue_count}개**",
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