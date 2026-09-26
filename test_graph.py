import msal, requests
CLIENT_ID = "7ed4cc2e-afe6-4bc5-82ca-0187a169d402"
app = msal.PublicClientApplication(CLIENT_ID, authority="https://login.microsoftonline.com/common")
flow = app.initiate_device_flow(scopes=["Mail.Read", "Calendars.Read"])
if "user_code" not in flow:
    print(flow.get("error"), "-", flow.get("error_description"))
    raise SystemExit
print(flow["message"])
token = app.acquire_token_by_device_flow(flow)["access_token"]
r = requests.get("https://graph.microsoft.com/v1.0/me/messages?$top=5&$select=subject",
                 headers={"Authorization": f"Bearer {token}"})
for m in r.json()["value"]: print(m["subject"])