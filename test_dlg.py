from kivymd.app import MDApp
from kivymd.uix.button import MDButton, MDButtonText
from kivymd.uix.dialog import MDDialog, MDDialogHeadlineText, MDDialogSupportingText, MDDialogButtonContainer
from kivy.clock import Clock

class TestApp(MDApp):
    def build(self):
        self.dialog = MDDialog(
            MDDialogHeadlineText(text='Missing API Key'),
            MDDialogSupportingText(text='No Gemini API key found.'),
            MDDialogButtonContainer(
                MDButton(MDButtonText(text='CANCEL'), style='text'),
                MDButton(MDButtonText(text='GO TO SETTINGS'), style='text')
            )
        )
        Clock.schedule_once(lambda dt: self.dialog.open(), 0.1)
        Clock.schedule_once(lambda dt: self.stop(), 0.5)
        return None

TestApp().run()
