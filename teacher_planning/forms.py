from django import forms
from .models import Teacher, DailySchedule

class TeacherForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = ['name', 'teaching_subjects', 'availability', 'on_duty']

class DailyScheduleForm(forms.ModelForm):
    class Meta:
        model = DailySchedule
        fields = ['teacher', 'day', 'start_time', 'end_time', 'class_length']
