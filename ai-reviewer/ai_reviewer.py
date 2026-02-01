import os
import json
import requests
import re
from github import Github, Auth
from openai import OpenAI

# ==============================================================================
# 1. 설정 및 초기화 (Configuration)
# ==============================================================================
class Config:
    LLM_API_KEY = os.getenv("LLM_API_KEY", "EMPTY")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-coder:7b") # "llama3", "deepseek-coder", "qwen2.5-coder:7b"
    
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
    PR_NUMBER = os.getenv("PR_NUMBER")
    REPO_NAME = os.getenv("GITHUB_REPOSITORY")
    WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
    
    IGNORED_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.svg', '.json', '.lock', '.pbxproj', '.xib', '.storyboard']

    @staticmethod
    def validate():
        if not Config.LLM_BASE_URL or not Config.LLM_MODEL:
            print("❌ Error: LLM_BASE_URL or LLM_MODEL is missing")
            exit(1)
        if not Config.PR_NUMBER:
            print("❌ Error: PR_NUMBER is missing")
            exit(1)

# ==============================================================================
# 2. 헬퍼 함수 (Utils)
# ==============================================================================
def get_valid_lines(patch):
    """
    Git Patch 텍스트를 파싱하여 코멘트 가능한(변경된) 라인 번호들의 집합(Set)을 반환합니다.
    """
    valid_lines = set()
    current_line_num = 0
    
    if not patch:
        return valid_lines

    for line in patch.split('\n'):
        if line.startswith('@@'):
            match = re.search(r'\+(\d+)', line)
            if match:
                current_line_num = int(match.group(1))
            continue
        
        if line.startswith(' ') or line.startswith('+'):
            valid_lines.add(current_line_num)
            current_line_num += 1
        elif line.startswith('-'):
            pass
            
    return valid_lines

def clean_json_text(text):
    """LLM 응답에서 마크다운이나 불필요한 공백을 제거하여 순수 JSON 문자열만 추출"""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

# ==============================================================================
# 3. LLM 통신 (LLM Interface)
# ==============================================================================
def call_llm(client, system_prompt, user_prompt, temperature=0.2):
    """LLM API 호출 공통 함수"""
    try:
        response = client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temperature
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ LLM 호출 실패: {e}")
        return None

# ==============================================================================
# 4. 리뷰 로직 (Core Logic)
# ==============================================================================
def analyze_file(client, file):
    """단일 파일에 대한 코드 리뷰 수행"""
    # 1. 파일 필터링
    if file.status == "removed" or file.patch is None:
        return [], None
        
    if any(file.filename.endswith(ext) for ext in Config.IGNORED_EXTENSIONS):
        print(f"🚫 Skipping (Ignored type): {file.filename}")
        return [], None

    print(f"🔍 Analyzing: {file.filename}")
    
    # 2. 유효 라인 계산
    valid_lines = get_valid_lines(file.patch)
    
    # 3. 프롬프트 구성
    system_prompt = "You are a code reviewer. You must output only valid JSON. Responses must be in Korean."
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

    # 4. LLM 호출
    response_text = call_llm(client, system_prompt, user_prompt)
    if not response_text:
        return [], file.patch

    # 5. 결과 파싱 및 검증
    comments = []
    try:
        cleaned_text = clean_json_text(response_text)
        data = json.loads(cleaned_text)
        if isinstance(data, dict): data = [data]

        for item in data:
            if 'line' not in item or 'message' not in item: continue
            
            try:
                line_num = int(item['line'])
            except ValueError: continue

            # Diff 범위 검사 (422 에러 방지)
            if line_num not in valid_lines:
                print(f"🚫 스킵: 라인 {line_num}은 Diff 범위 밖입니다.")
                continue

            # 포맷팅
            icon = "⚠️" if item.get('category') == '이슈' else "💡"
            severity = item.get('severity', 'Minor')
            severity_icon = "🔥" if severity == 'Critical' else "🔴" if severity == 'Major' else "🟡"
            
            body = f"### {icon} {item.get('category', '리뷰')} | {severity_icon} {severity}\n\n{item['message']}"
            
            comments.append({
                "path": file.filename,
                "line": line_num,
                "body": body
            })
            
    except json.JSONDecodeError:
        print(f"⚠️ JSON 파싱 실패 ({file.filename})")
    except Exception as e:
        print(f"⚠️ 에러 발생 ({file.filename}): {e}")

    return comments, file.patch

# ==============================================================================
# 5. GitHub 작업 (GitHub Actions)
# ==============================================================================
def post_review_comments(pr, last_commit, comments):
    """수집된 리뷰 코멘트들을 GitHub PR에 등록"""
    if not comments:
        return
    
    print(f"📨 {len(comments)}개의 인라인 코멘트 등록 중...")
    try:
        pr.create_review(
            commit=last_commit,
            body=f"## 🤖 {Config.LLM_MODEL} Code Review\n리뷰가 도착했습니다! 코드를 확인해주세요.",
            event="COMMENT",
            comments=comments
        )
        print("✅ 인라인 리뷰 등록 완료!")
    except Exception as e:
        print(f"❌ 리뷰 등록 실패: {e}")

def update_pr_description(client, pr, all_diffs_context):
    """PR 본문(Description) 요약 업데이트"""
    print("📝 전체 변경 사항 요약 중...")
    
    summary_prompt = f"""
    너는 테크 리드야. 전체 코드 변경 사항을 보고 PR 설명을 작성해. (한국어)
    1. 📌 3줄 요약
    2. 🔍 주요 변경점 (글머리 기호)
    
    --- Diff Context (Truncated) ---
    {all_diffs_context[:30000]}
    """
    
    summary_text = call_llm(client, "You are a helpful assistant.", summary_prompt, temperature=0.5)
    if not summary_text:
        return

    try:
        marker_start = ""
        marker_end = ""
        
        current_body = pr.body if pr.body else ""
        new_section = f"{marker_start}\n## 🤖 AI 요약 ({Config.LLM_MODEL})\n\n{summary_text}\n{marker_end}"
        
        # 기존 AI 요약이 있으면 교체, 없으면 상단 추가
        if marker_start in current_body and marker_end in current_body:
            pattern = re.compile(f"{re.escape(marker_start)}.*?{re.escape(marker_end)}", re.DOTALL)
            final_body = pattern.sub(new_section, current_body)
        else:
            final_body = f"{new_section}\n\n{current_body}"

        pr.edit(body=final_body)
        print("✅ PR 본문 업데이트 완료!")
    except Exception as e:
        print(f"❌ PR 요약 업데이트 실패: {e}")

# ==============================================================================
# 6. 알림 (Notification)
# ==============================================================================
def send_discord_notification(pr, issue_count):
    """디스코드 알림 전송"""
    if not Config.WEBHOOK_URL:
        return

    print("🔔 디스코드 알림 전송 중...")
    try:
        payload = {
            "username": "AI Code Reviewer",
            "avatar_url": "[https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png](https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png)",
            "embeds": [{
                "title": f"🤖 AI 리뷰 완료: #{Config.PR_NUMBER} {pr.title}",
                "url": pr.html_url,
                "color": 5814783,
                "fields": [{
                    "name": "📊 분석 결과",
                    "value": f"모델: {Config.LLM_MODEL}\n코멘트: **{issue_count}개**",
                    "inline": True
                }],
                "footer": {"text": f"Repo: {Config.REPO_NAME}"}
            }]
        }
        requests.post(Config.WEBHOOK_URL, json=payload)
        print("✅ 디스코드 알림 전송 완료!")
    except Exception as e:
        print(f"❌ 디스코드 전송 실패: {e}")

# ==============================================================================
# 7. 메인 실행 (Main Execution)
# ==============================================================================
def main():
    print(f"🐍 스크립트 시작... (Model: {Config.LLM_MODEL})", flush=True)
    Config.validate()

    # 클라이언트 초기화
    client = OpenAI(api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL)
    g = Github(auth=Auth.Token(Config.GITHUB_TOKEN))
    repo = g.get_repo(Config.REPO_NAME)
    pr = repo.get_pull(int(Config.PR_NUMBER))
    last_commit = list(pr.get_commits())[-1]

    all_comments = []
    all_diffs_context = ""
    
    # 파일별 리뷰 수행
    for file in pr.get_files():
        comments, patch = analyze_file(client, file)
        
        if comments:
            all_comments.extend(comments)
        
        # 요약을 위한 Diff 수집 (최대 30000자)
        if patch and len(all_diffs_context) < 30000:
            all_diffs_context += f"\n\n--- File: {file.filename} ---\n{patch}"

    # GitHub에 코멘트 등록
    if all_comments:
        post_review_comments(pr, last_commit, all_comments)
    else:
        print("✨ 발견된 이슈가 없거나, AI가 코멘트를 생성하지 않았습니다.")

    # PR 본문 요약 업데이트
    if all_diffs_context:
        update_pr_description(client, pr, all_diffs_context)

    # 디스코드 알림
    send_discord_notification(pr, len(all_comments))

if __name__ == "__main__":
    main()