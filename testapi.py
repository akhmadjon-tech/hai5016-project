import os
from dotenv import load_dotenv
from openai import OpenAI


# Load environment variables from .env file
load_dotenv()

client = OpenAI(
    base_url=os.getenv("AZURE_FOUNDRY_ENDPOINT"),
    api_key=os.getenv("AZURE_FOUNDRY_API_KEY"),
)

completion = client.chat.completions.create(
    model=os.getenv("AZURE_FOUNDRY_MODEL"),
    messages=[
        {
            "role": "user",
            "content": "How many R's are there in the word 'raspberry'?",
        }
    ],
)

# Print the response
print("Question: How many R's are there in the word 'raspberry'?")
print(f"Answer: {completion.choices[0].message.content}")
