import os
import google.generativeai as genai
from github import Github, Auth

# 1. 설정값 가져오기
gemini_api_key = os.getenv("GEMINI_API_KEY")
github_token = os.getenv("GITHUB_TOKEN")
repo_name = os.getenv("GITHUB_REPOSITORY")
pr_number_str = os.getenv("PR_NUMBER")

# ✅ 디버깅용: 키가 제대로 들어왔는지 확인 (보안상 앞 4자리만 출력)
if gemini_api_key:
    print(f"🔑 Gemini Key Check: {gemini_api_key[:4]}****")
else:
    print("❌ Error: GEMINI_API_KEY is None!")
    exit(1) # 강제 종료

if not pr_number_str:
    print("❌ Error: PR_NUMBER is missing!")
    exit(1)

pr_number = int(pr_number_str)

# 2. Gemini 설정 (Gemini 1.5 Flash 모델 사용)
genai.configure(api_key=gemini_api_key)
available_models = []
try:
    for m in genai.list_models():
        # 'generateContent' 기능을 지원하는 모델만 출력
        if 'generateContent' in m.supported_generation_methods:
            print(f" - {m.name}")
            available_models.append(m.name)
except Exception as e:
    print(f"⚠️ 모델 목록 조회 중 에러 발생: {e}")

print("---------------------------------------------------------\n")
model = genai.GenerativeModel('gemini-2.0-flash-lite')

# 3. GitHub PR 정보 가져오기
auth = Auth.Token(github_token)
g = Github(github_token)
repo = g.get_repo(repo_name)
pr = repo.get_pull(pr_number)

# 4. 변경된 파일(Diff) 가져오기
diff_content = ""
for file in pr.get_files():
    # 삭제된 파일이나 너무 큰 파일은 건너뛰기 가능
    if file.status == "removed":
        continue
    
    diff_content += f"\n\n--- File: {file.filename} ---\n"
    diff_content += file.patch if file.patch else "(No content change)"

# 5. Gemini에게 리뷰 요청할 프롬프트 작성
prompt = f"""
너는 시니어 iOS 개발자야. 아래 변경된 코드(Diff)를 보고 코드 리뷰를 해줘.
리뷰 강도는 '높음' 수준으로 해줘
반드시 한국어로 답변하고, 다음 형식을 지켜줘:

1. **요약**: 변경 사항을 한 줄로 요약
2. **주요 변경점**: 핵심적인 변경 사항 설명
3. **개선 제안**: 버그 가능성, 성능 문제, 혹은 Swift 스타일 가이드 위반 사항이 있다면 구체적으로 지적 (없다면 생략 가능)
4. **칭찬**: 잘 짜여진 코드가 있다면 언급

--- 변경된 코드 ---
{diff_content[:50000]} 
""" 
# (참고: Gemini는 입력량이 많지만, 혹시 몰라 3만 자로 자름. 필요 시 조절 가능)

try:
    # 6. Gemini에게 질문
    response = model.generate_content(prompt)
    review_result = response.text

    # 7. PR에 댓글 달기
    pr.create_issue_comment(f"## 🤖 Gemini AI Code Review\n\n{review_result}")
    print("✅ 리뷰 등록 완료!")

except Exception as e:
    print(f"❌ 에러 발생: {e}")