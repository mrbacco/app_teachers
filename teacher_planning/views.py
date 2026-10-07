



from django.shortcuts import render, redirect
from .models import Teacher, DailySchedule
from .forms import TeacherForm, DailyScheduleForm

def teacher_list(request):
    teachers = Teacher.objects.all()
    context = {
        'teachers': teachers
    }
    return render(request, 'teacher_list.html', context)


def teacher_detail(request, pk):
    teacher = Teacher.objects.get(pk=pk)
    return render(request, 'teacher_planning/teacher_detail.html', {'teacher': teacher})

def teacher_new(request):
    if request.method == "POST":
        form = TeacherForm(request.POST)
        if form.is_valid():
            teacher = form.save(commit=False)
            teacher.save()
            return redirect('teacher_detail', pk=teacher.pk)
    else:
        form = TeacherForm()
    return render(request, 'teacher_planning/teacher_edit.html', {'form': form})

def teacher_edit(request, pk):
    teacher = Teacher.objects.get(pk=pk)
    if request.method == "POST":
        form = TeacherForm(request.POST, instance=teacher)
       


