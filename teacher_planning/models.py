



# Create your models here.
from django.db import models
from django.utils import timezone



class Teacher(models.Model):
    name = models.CharField(max_length=255)
    teaching_subjects = models.CharField(max_length=255)
    availability = models.BooleanField(default=True)
    on_duty = models.BooleanField(default=True)
    
    def __str__(self):
        return self.name
    
class DailySchedule(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    day = models.DateField(default=timezone.now)
    start_time = models.TimeField()
    end_time = models.TimeField()
    class_length = models.PositiveIntegerField(default=50)
    
    def __str__(self):
        return f"{self.teacher.name}'s schedule for {self.day}"

class Schedule(models.Model):
    DAY_CHOICES = (
        ('MON', 'Monday'),
        ('TUE', 'Tuesday'),
        ('WED', 'Wednesday'),
        ('THU', 'Thursday'),
        ('FRI', 'Friday'),
    )

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    day = models.CharField(choices=DAY_CHOICES, max_length=3)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_available = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.teacher.name}'s schedule on {self.day}"

class Subject(models.Model):
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Availability(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    day = models.CharField(max_length=10)
    available = models.BooleanField(default=True)

class OnOffDuty(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    day = models.CharField(max_length=10)
    on_duty = models.BooleanField(default=True)
