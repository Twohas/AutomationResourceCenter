import os
import json
import re
import google.generativeai as genai
from github import Github, Auth

# 1. 설정값 가져오기
gemini_api_key = os.getenv("GEMINI_API_KEY")
github_token = os.getenv("GITHUB_TOKEN")
repo_name = os.getenv("GITHUB_REPOSITORY")
pr_number_str = os.getenv("PR_NUMBER")

# 유효성 검사
if not gemini_api_key:
    print("❌ Error: GEMINI_API_KEY is missing")
    exit(1)
if not pr_number_str:
    print("❌ Error: PR_NUMBER is missing")
    exit(1)

# 2. Gemini 설정 (Gemini 1.5 Flash 모델 사용)
genai.configure(api_key=gemini_api_key)
model = genai.GenerativeModel("gemini-2.5-flash", generation_config={"response_mime_type": "application/json"})
auth = Auth.Token(github_token)
g = Github(auth=auth)
repo = g.get_repo(repo_name)
pr = repo.get_pull(int(pr_number_str))
last_commit = list(pr.get_commits())[-1]

print("🚀 리뷰 시작 (Model: gemini-1.5-flash)")

# 3. 변경된 파일별로 리뷰 데이터 수집
review_comments = []

for file in pr.get_files():
    if file.status == "removed" or file.patch is None:
        continue
    
    print(f"🔍 Analyzing: {file.filename}")

    # 프롬프트 (CodeRabbit 스타일)
    prompt = f"""
    너는 구글, 애플 출신의 시니어 개발자야. 아래 제공되는 Git Diff 코드를 분석해서 코드 리뷰를 해줘.
    
    **파일명:** {file.filename}
    
    **목표:**
    1. 버그, 성능 이슈, 스타일 가이드 위반, 안티 패턴을 찾아내.
    2. 칭찬할 점이 있다면 칭찬해.
    3. 중요하지 않은 변경사항은 무시해. (리뷰 노이즈 최소화)

    **출력 형식 (JSON List):**
    반드시 아래 JSON 구조의 리스트로만 응답해. 마크다운 코드블럭을 쓰지 말고 순수 JSON만 출력해.
    
    [
      {{
        "line": <int: 이슈가 발견된 변경 후 파일의 라인 번호>,
        "category": "<string: '이슈' | '제안' | '칭찬'>",
        "severity": "<string: 'Critical' | ''Major' | 'Minor' | 'Info'>",
        "message": "<string: 리뷰 내용 (한국어)>"
      }}
    ]

    **코멘트 스타일 가이드:**
    - CodeRabbit 스타일을 따라해.
    - 친절하지만 명확하게 설명해.

    --- Git Diff ---
    {file.patch}
    """

    try:
        response = model.generate_content(prompt)
        response_text = response.text.strip()
        
        if response_text.startswith("```json"):
            response_text = response_text[7:-3]
        elif response_text.startswith("```"):
            response_text = response_text[3:-3]
            
        comments_data = json.loads(response_text)

        for item in comments_data:
            icon = "📝"
            if item['category'] == '이슈': icon = "⚠️"
            elif item['category'] == '칭찬': icon = "🙌"
            elif item['category'] == '제안': icon = "💡"

            severity_icon = "⚪️"
            if item['severity'] == 'Critical': severity_icon = "🔥" # Critical은 불꽃 아이콘
            elif item['severity'] == 'Major': severity_icon = "🔴"
            elif item['severity'] == 'Minor': severity_icon = "🟡"

            body = f"### {icon} {item['category']} | {severity_icon} {item['severity']}\n\n{item['message']}"

            review_comments.append({
                "path": file.filename,
                "line": int(item['line']),
                "body": body
            })

    except Exception as e:
        print(f"⚠️ {file.filename} 처리 중 에러: {e}")
        continue

# 4. 리뷰 등록
if review_comments:
    try:
        print(f"📨 총 {len(review_comments)}개의 코멘트를 등록합니다...")
        pr.create_review(
            commit=last_commit,
            body="## 🤖 Gemini AI Code Review\n리뷰가 도착했습니다! 코드를 확인해주세요.",
            event="COMMENT",
            comments=review_comments
        )
        print("✅ 인라인 리뷰 등록 완료!")
        
    except Exception as e:
        print(f"❌ 리뷰 등록 실패: {e}")
else:
    print("✅ 발견된 이슈가 없습니다.")