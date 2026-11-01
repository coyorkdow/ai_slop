(function () {
  var form = document.getElementById('batch');
  if (!form) return;
  var boxes = document.querySelectorAll('input[name="path"][form="batch"]');
  var label = form.querySelector('.selected');
  function checked() {
    return Array.prototype.filter.call(boxes, function (box) { return box.checked; }).length;
  }
  function update() { label.textContent = '已选 ' + checked() + ' 个'; }
  Array.prototype.forEach.call(boxes, function (box) { box.addEventListener('change', update); });
  Array.prototype.forEach.call(form.querySelectorAll('[data-select]'), function (button) {
    button.addEventListener('click', function () {
      var all = button.getAttribute('data-select') === 'all';
      Array.prototype.forEach.call(boxes, function (box) { box.checked = all; });
      update();
    });
  });
  Array.prototype.forEach.call(form.querySelectorAll('[data-delete]'), function (button) {
    button.addEventListener('click', function (event) {
      var kind = button.getAttribute('data-delete');
      var count = checked();
      var message = kind === 'original'
        ? '删除选中的 ' + count + ' 个原始视频？已有压缩版的会保留压缩版；没有压缩版的录像会从列表中消失。'
        : '删除选中的 ' + count + ' 个压缩版及画面变化索引？原始视频还在的会重新压缩；原始视频已删除的录像会被永久移除。';
      if (!count) {
        event.preventDefault();
        alert('请先勾选录像');
      } else if (!confirm(message)) {
        event.preventDefault();
      } else {
        form.elements['delete'].value = kind;
      }
    });
  });
  update();
})();
